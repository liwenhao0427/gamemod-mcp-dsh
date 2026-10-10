"""会话状态：把「发现游戏 → 建索引 → 注入运行时」串成一条可复用的会话。

为什么要单独做这一层
--------------------------------------------------
MCP 工具是无状态的函数调用，但 Mod 开发天然是有状态的：
Agent 先选定一个游戏，然后反复查询它的类、Hook 它的方法。
如果每个工具调用都重新解析元数据、重新注入进程，既慢又会重复 Hook。

因此这里维护一个**进程级单例会话**，记录：
    · 当前选中的游戏目录与引擎类型
    · 已打开的元数据索引（含查询结果句柄表）
    · 已注入的运行时会话（Frida）
并把「当前游戏」作为所有工具的隐式上下文。

与 AiMod 的对应关系
--------------------------------------------------
AiMod 的流程是：detecting → reuse/injecting → verify → mono_snapshot →
mcp_start → final_verify，并且有 session_lock 保证单会话。
本模块的 `ensure_*` 系列方法扮演同样角色，只是把「云 + 订阅校验」去掉，
换成纯本地、可自由重入的实现。
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

from ..logutil import progress, warn
from . import discovery
from .il2cpp_meta import MetadataError
from .index import Index, ResultStore
from .pe import summarize as pe_summarize

# ---------------------------------------------------------------- 状态对象


class SessionState:
    """当前会话状态（进程内单例）。"""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.game_dir: str = ""
        self.engine: str = ""
        self.exe: str = ""
        self.metadata_path: str = ""
        self._index: Index | None = None
        # 响应上限（P2-4）截断下来的完整列表放这里，而不是放进 Index 的句柄表：
        # 那样的话「没有索引」的工具（纯运行时那几个）就没有句柄可用，
        # 而这里的东西必须在任何工具下都能登记。
        # `close_index` 会清掉它，于是「切游戏 ⇒ 旧句柄全失效」是一条统一规则。
        self.cap_results = ResultStore(owner="cap")
        self.runtime = None            # RuntimeSession 或 None
        self.notes: list[str] = []     # 操作留痕，便于 Agent 自查
        self.opened_at: float = 0.0

    # ------------------------------------------------ 留痕

    def note(self, text: str) -> None:
        self.notes.append(f"[{time.strftime('%H:%M:%S')}] {text}")
        if len(self.notes) > 200:
            del self.notes[:100]

    # ------------------------------------------------ 索引

    @property
    def index(self) -> Index | None:
        return self._index

    def has_index(self) -> bool:
        return self._index is not None

    def close_index(self) -> dict:
        """关闭索引。**返回结果**（以前只 warn，失败对上层不可见）。"""
        # 索引句柄与 cap 句柄一起作废：切游戏后旧句柄一律失效是一条统一规则，
        # 不能让「cap 句柄」成为例外（它的数据同样属于上一个游戏）。
        # 注意放在 early-return **之前** —— 纯运行时工具也会往这里登记句柄，
        # 那时 _index 可能本来就是 None。
        self.cap_results.clear()
        if self._index is None:
            return {"ok": True, "already_closed": True}
        try:
            self._index.close()
        except Exception as exc:
            warn(f"关闭索引失败：{exc}")
            # 失败时不摘引用，便于重试；同时把原因交给上层
            return {"ok": False, "error": f"关闭索引失败：{exc}"}
        self._index = None
        return {"ok": True}

    # ------------------------------------------------ 运行时

    def has_runtime(self) -> bool:
        return self.runtime is not None and getattr(self.runtime, "alive", False)

    def close_runtime(self) -> dict:
        if self.runtime is None:
            return {"ok": True, "already_closed": True}
        try:
            res = self.runtime.close()
        except Exception as exc:
            warn(f"关闭运行时会话失败：{exc}")
            # 失败时**保留** self.runtime：会话可能只是没 detach 干净，
            # 保留引用调用方才能重试。以前这里用 finally 无条件清空，
            # 于是「关闭失败」之后连重试的入口都没有了。
            return {"ok": False, "error": str(exc)}
        if res.get("ok"):
            self.runtime = None
        elif res.get("session_released"):
            # 探针卡死：`close()` 如实报 ok:False（游戏**没**恢复原状），
            # 但那个会话对象已经作废、登记也摘掉了 —— 留着引用只会让
            # `has_runtime()` 继续报 True，而后续每个 runtime_* 都撞
            # 「会话已失效」。所以这里按「会话已释放」摘掉引用，
            # 同时**把失败原因带出去**（不能因为摘了引用就当成功）。
            warn(f"运行时会话已释放但未恢复原状：{res.get('error')}")
            self.runtime = None
        else:
            warn(f"运行时会话未完全关闭：{res.get('error')}")
        return res

    # ------------------------------------------------ 切换游戏

    def reset_target(self) -> None:
        """清空「当前游戏是谁」的身份信息（切换游戏时调用）。

        必须**显式清空**，而不是指望每个分支各自把字段写对：
        `select_game` 的 IL2CPP 分支只写 `metadata_path`、不写 `exe`，
        于是上一个会话的 `exe` 会原样留下来 —— 实测同一进程内

            open_game(某个 Mono 游戏)   → exe = <Mono 游戏>\\Xxx_Data\\Managed
            open_game(某个 IL2CPP 游戏) → exe 仍然是上面那个 Mono 目录

        而 `exe` 正是 `launch_game` / `ue_profile` / `detect_ue` 不传参时
        默认使用的目标，等于**可能对另一个游戏动手**（最坏是启动并注入错游戏）。
        新进程首次 open_game 看不出这个问题，只有切游戏时才暴露，
        所以固定在这里清一次。
        """
        self.game_dir = ""
        self.engine = ""
        self.exe = ""
        self.metadata_path = ""
        self.opened_at = 0.0

    # ------------------------------------------------ 概要

    def summary(self) -> dict[str, Any]:
        rt = None
        if self.runtime is not None:
            rt = {
                "pid": self.runtime.pid,
                "name": self.runtime.name,
                "alive": self.runtime.alive,
                "spawned": self.runtime.spawned,
            }
        idx_info = None
        if self._index is not None:
            try:
                idx_info = self._index.index_info()
            except Exception as exc:
                idx_info = {"error": str(exc)}
        return {
            "game_dir": self.game_dir,
            "engine": self.engine,
            "engine_label": _engine_label(self.engine),
            "exe": self.exe,
            "metadata_path": self.metadata_path,
            "index_ready": self.has_index(),
            "index": idx_info,
            "runtime": rt,
            "runtime_ready": self.has_runtime(),
            "notes": self.notes[-20:],
        }


_state = SessionState()


# 引擎名 → 给 Agent 看的说明。这份说明会随 open_game 一起返回，
# 让 Agent 一开始就知道该引擎「元数据里有什么、没有什么」。
_ENGINE_LABELS = {
    "il2cpp": "IL2CPP (Unity AOT) — 逻辑在 GameAssembly.dll，元数据是 global-metadata.dat",
    "mono": "Mono (Unity JIT / .NET) — 逻辑在托管 dll 里，可直接反编译成 C#",
    "ue": "Unreal Engine — 无集中元数据，结构信息依赖 UE profile / .usmap",
    "unity": "Unity（未能进一步区分 IL2CPP 还是 Mono）",
}


def _engine_label(engine: str) -> str:
    """引擎的可读说明；未知引擎原样返回，不编。"""
    return _ENGINE_LABELS.get(engine, engine or "未识别")


def state() -> SessionState:
    """取当前会话状态。"""
    return _state


# ---------------------------------------------------------------- 选择游戏


def _same_path(a: str, b: str) -> bool:
    """比较两个路径是否指向同一个文件/目录（容错大小写与相对路径）。"""
    if not a or not b:
        return False
    try:
        return os.path.normcase(os.path.abspath(a)) == \
               os.path.normcase(os.path.abspath(b))
    except Exception:
        return False


def plan_selection(
    game_dir: str = "",
    engine: str = "",
    metadata_path: str = "",
) -> dict:
    """**只读地**算出「选中这个游戏之后会话会变成什么样」，不改动任何会话状态。

    为什么需要它（缺陷 #3 的结构性修复）
    --------------------------------------------------
    `select_game` 过去是「先收拾旧会话、再逐个校验」，于是**任何**后续失败
    （包括调用方在它返回之后才做的判定）都会留下一个被改坏的会话。
    实测现场：`open_game(game_dir="E:\\project")` 返回 `ok:false`（正确拒绝了
    容器目录），但 `get_session()` 显示 `game_dir` 已经变成 `E:\\project`、
    `exe` 被填成了子目录里某个游戏的主程序 —— 而 `launch_game()` 不传参时
    默认就用会话里的 `exe`，等于会去启动容器下的某一款游戏。

    所以把「算」与「提交」拆开：本函数**只读**，`commit_selection` 才写。
    调用方（`server.open_game`）因此可以先把「引擎受不受支持」这类判定做完，
    确认没问题再提交 —— 失败路径在结构上就不可能带副作用，
    而不是靠每个失败分支自己记得回滚。

    Returns:
        {ok: True, reused: True}                      —— 同一个游戏，无需改动
        {ok: True, plan: {...}}                       —— 可以提交，plan 是目标身份
        {ok: False, error: ..., ...}                  —— 探测/校验失败，会话未被触碰
    """
    with _state.lock:
        # 先判断这次是不是在选同一个游戏；是的话直接复用，什么都不动
        want_meta = metadata_path
        want_dir = game_dir
        # `*_Data` 目录只列一次，后面几个判断（引擎识别 / 找元数据）都复用它。
        # 容器目录上列一遍要 ~130 ms，一个 open_game 里问四遍没有必要。
        want_data_dirs = (discovery.list_data_dirs(Path(want_dir))
                          if want_dir and Path(want_dir).is_dir() else [])
        if not want_meta and want_dir:
            # 目录模式下先解析出元数据路径，才能判断是否同一个游戏
            try:
                det = engine or discovery.detect_engine(want_dir, want_data_dirs)
                if det == "il2cpp":
                    want_meta = discovery.il2cpp_paths(
                        want_dir, want_data_dirs).get("metadata", "")
                elif det == "mono":
                    # Mono 的索引对象是单个程序集，同样要解析出来才能比对
                    mono = discovery.mono_paths(want_dir, want_data_dirs)
                    want_meta = _pick_mono_assembly(mono.get("assemblies") or [])
            except Exception:
                want_meta = ""

        # 复用条件：元数据文件相同 **且** 引擎没被指定成另一个。
        # 只比路径是不够的 —— 同一个文件可以被两种解析器读，而两种读法
        # 给出的结果完全不同（IL2CPP 器读 Mono 程序集只会得到空结果）。
        want_engine = (engine or "").strip().lower()
        same_engine = (not want_engine
                       or want_engine == _state.engine
                       or want_engine in ("auto", "unknown"))
        if want_meta and _same_path(want_meta, _state.metadata_path) \
                and _state.has_index() and same_engine:
            return {"ok": True, "reused": True}

        # ---- 以下全部是**只读**探测与校验，一个字节都不写进 _state ----
        if metadata_path:
            mp = Path(metadata_path)
            if not mp.is_file():
                return {"ok": False,
                        "error": f"元数据文件不存在：{metadata_path}"}
            sniffed = engine or _sniff_engine_for(mp)
            return {"ok": True, "plan": {
                "game_dir": str(mp.parent),
                # 显式指定引擎就用它；否则按文件内容嗅探（global-metadata.dat 的
                # magic vs .NET 程序集的 MZ 头），不要一律当成 il2cpp。
                "engine": sniffed,
                "metadata_path": str(mp),
                # 只知道元数据文件、不知道游戏目录时不去猜主程序：
                # 猜错会让 launch_game 拿着一个跑不起来的 exe 去启动。
                # 需要启动就用 launch_game(exe=...)。
                "exe": "",
                "note": f"选定元数据文件 {mp.name}（引擎 {sniffed}）",
            }}

        if not game_dir:
            return {"ok": False, "error": "必须提供 game_dir 或 metadata_path"}

        gd = Path(game_dir)
        if not gd.is_dir():
            return {"ok": False, "error": f"游戏目录不存在：{game_dir}"}

        detected = engine or discovery.detect_engine(gd, want_data_dirs)
        meta = ""
        exe = ""

        # 按引擎类型定位关键文件（全部是只读探测）
        if detected == "il2cpp":
            paths = discovery.il2cpp_paths(gd, want_data_dirs)
            meta = paths.get("metadata", "")
            if not meta:
                # 允许用户手工指定（有些游戏目录结构不标准）
                out = {
                    "ok": False,
                    "engine": detected,
                    "error": "识别为 IL2CPP，但没找到 global-metadata.dat。"
                             "请用 metadata_path 参数直接指定该文件的路径。",
                    "hint": paths,
                }
                # 多个候选游戏根 → 这是个容器目录。必须说清楚，否则调用方
                # 只会看到「没找到元数据」，然后在错误的目录上继续折腾。
                container = discovery.container_candidates(gd, want_data_dirs)
                if container:
                    out["error"] = (
                        f"这个目录下有 {container['count']} 个各带 IL2CPP 元数据的游戏，"
                        "它本身不是某一个游戏的安装目录 —— 请把 game_dir 指向其中某一个。"
                    )
                    out["container"] = container
                return out
        elif detected == "mono":
            mono = discovery.mono_paths(gd, want_data_dirs)
            managed = mono.get("managed_dir", "")
            # Mono 的元数据单位是**单个程序集**而不是目录，
            # 所以这里挑出「游戏自己的代码」那个 dll 作为索引对象。
            # 通常就是 Assembly-CSharp.dll；找不到则退到最大的非框架程序集。
            meta = _pick_mono_assembly(mono.get("assemblies") or [])
            if not meta:
                out = {
                    "ok": False,
                    "engine": detected,
                    "error": "识别为 Mono，但没找到可索引的托管程序集。"
                             "请用 metadata_path 参数直接指定某个 .dll。",
                    "hint": {"managed_dir": managed},
                }
                container = discovery.container_candidates(gd, want_data_dirs)
                if container:
                    out["error"] = (
                        f"这个目录下有 {container['count']} 个 Mono 游戏，"
                        "它本身不是某一个游戏的安装目录 —— 请把 game_dir 指向其中某一个。"
                    )
                    out["container"] = container
                return out
            # 这里**不**把 Managed 目录当成 exe：Mono 游戏的主程序仍然是游戏 exe。
            # 旧写法把目录当 exe，导致 inject_process 按主程序名找进程时拿
            # "Managed" 去比对，永远匹配不上。留空由下面统一兜底。
            exe = ""
        elif detected == "ue":
            ue = discovery.ue_paths(gd)
            exe = ue.get("exe", "")

        if not exe:
            exe = discovery.pick_main_exe(gd)

        return {"ok": True, "plan": {
            "game_dir": str(gd),
            "engine": detected,
            "metadata_path": meta,
            "exe": exe,
            "note": f"选定游戏 {gd.name}（引擎 {detected}）",
            "detected_engine": detected,
        }}


def select_game(
    game_dir: str = "",
    engine: str = "",
    metadata_path: str = "",
) -> dict:
    """选定一个游戏作为后续操作的上下文（= 规划 + 提交）。

    允许只给 game_dir（自动识别引擎），也允许直接给 metadata_path
    （适合只做离线分析、不装游戏的场景）。

    **重复选中同一个游戏不会重建会话** —— 否则每次 open_game
    都会把已经建好的索引和已注入的探针扔掉，Agent 反复调用就会反复重建。

    主程序 exe 一律自动挑（`discovery.pick_main_exe`），本函数不接受指定：
    需要换一个 exe 启动时用 `launch_game(exe=...)`，那才是启动参数该待的地方。
    （原先这里有个 `exe` 形参，但没有任何调用方传过它 —— 见扫描报告 §3.7。）

    **需要「先判定、再提交」的调用方请用 `plan_selection` + `commit_selection`**：
    `server.open_game` 就是那样做的，因为「引擎受不受支持」这个判定发生在
    本函数之外，若先提交再判定，失败就会留下被改坏的会话（缺陷 #3）。
    """
    res = plan_selection(game_dir=game_dir, engine=engine,
                         metadata_path=metadata_path)
    if not res.get("ok"):
        return res
    if res.get("reused"):
        with _state.lock:
            _state.note("重复选中同一个游戏，复用现有会话")
            return {"ok": True, "reused": True, **_state.summary()}
    return commit_selection(res["plan"])


def commit_selection(plan: dict[str, Any]) -> dict:
    """提交一次**已通过校验**的选游戏：收拾旧资源 → 写入新身份 → 返回摘要。

    **只允许在 `plan_selection` 之后、且调用方自己的判定也通过之后调用**。
    这样「失败的 open_game 污染会话」在结构上就不可能发生 ——
    而不是靠每个失败分支自己记得回滚（缺陷 #3 的根因就是后者）。
    """
    with _state.lock:
        _state.close_runtime()
        _state.close_index()
        _state.notes.clear()
        # 身份信息也必须清 —— 否则「上一个游戏的 exe」会被当成这个游戏的
        # （IL2CPP 分支不写 exe），详见 reset_target 的注释。
        _state.reset_target()
        _state.game_dir = plan.get("game_dir", "")
        _state.engine = plan.get("engine", "")
        _state.metadata_path = plan.get("metadata_path", "")
        _state.exe = plan.get("exe", "")
        _state.opened_at = time.time()
        note = plan.get("note")
        if note:
            _state.note(note)
        out = {"ok": True, **_state.summary()}
        if plan.get("detected_engine"):
            out["detected_engine"] = plan["detected_engine"]
        return out


# ---------------------------------------------------------------- 引擎嗅探

# IL2CPP global-metadata.dat 的文件头 magic
_IL2CPP_MAGIC = 0xFAB11BAF


def _sniff_engine_for(path: Path) -> str:
    """按文件内容判断该用哪个解析器。

    只在用户没显式指定 engine 时使用。判断依据很硬：
        · global-metadata.dat  → 以 0xFAB11BAF 开头
        · .NET 程序集          → 以 "MZ" 开头（标准 PE）
    判断不出来时**不猜**，返回空串，让上层给出可读的报错。
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(4)
    except OSError:
        return ""
    if len(head) >= 4 and int.from_bytes(head, "little") == _IL2CPP_MAGIC:
        return "il2cpp"
    if head[:2] == b"MZ":
        return "mono"
    return ""


# ---------------------------------------------------------------- Mono 程序集挑选
#
# 实现放在 discovery.py（那里也有 mono_paths，两者是配套的）。
# 这里只做转发，避免「哪些 dll 是框架、哪个是游戏代码」这份知识
# 在两处各写一遍，然后慢慢走偏。
_pick_mono_assembly = discovery.pick_mono_assembly


# ---------------------------------------------------------------- 建索引


def ensure_index(
    metadata_path: str = "",
    cache_dir: str = "",
    force_rebuild: bool = False,
    game_dir: str = "",
) -> dict:
    """保证当前游戏有可用的元数据索引（必要时构建）。

    这是所有查询类工具的前置步骤。已建好则直接复用。
    """
    with _state.lock:
        # 允许一次调用里直接把游戏也选好；已经选好同一个游戏则不动。
        if game_dir or metadata_path:
            sel = select_game(game_dir=game_dir, metadata_path=metadata_path)
            if not sel.get("ok"):
                return sel
            if sel.get("reused") and not force_rebuild:
                return {"ok": True, "reused": True, **_state.summary()}

        if _state.has_index() and not force_rebuild:
            return {"ok": True, "reused": True, **_state.summary()}

        if not _state.metadata_path:
            return {
                "ok": False,
                "error": "当前会话没有元数据文件。请先用 select_game 选定游戏，"
                         "或用 metadata_path 指定 global-metadata.dat。",
                **_state.summary(),
            }

        mp = Path(_state.metadata_path)
        if not mp.is_file():
            return {"ok": False, "error": f"元数据文件不存在：{mp}"}

        # 重建前必须先关掉旧连接，否则 Windows 上删不掉被占用的 sqlite 文件
        if force_rebuild and _state.has_index():
            _state.close_index()

        try:
            t0 = time.time()
            idx = Index.open(mp,
                             cache_dir=cache_dir or None,
                             force_rebuild=force_rebuild,
                             engine=_state.engine)
            _state._index = idx
            dt = time.time() - t0
            _state.note(f"索引就绪（{dt:.2f}s）")
            progress(f"索引就绪，耗时 {dt:.2f}s")
            out = {"ok": True, "reused": False, "elapsed": round(dt, 3),
                   **_state.summary()}
            return out
        except MetadataError as exc:
            return {"ok": False, "error": f"元数据解析失败：{exc}"}
        except Exception as exc:
            return {"ok": False, "error": f"构建索引失败：{exc}"}


# ---------------------------------------------------------------- 运行时


#: spawn 之后等 IL2CPP 就绪的等待预算（秒）。
#:
#: 为什么按引擎给不同预算：托管运行时（`GameAssembly.dll` / `mono-2.0-bdwgc.dll`）
#: 是**慢慢**加载起来的，刚 spawn 完第一次探测几乎必然是「未找到运行时模块」，
#: 所以 IL2CPP 与 Mono 都必须给足时间（实测 GoW(IL2CPP) 约 3 秒、
#: Echoes 约 6 秒就绪；Mono 侧要等 `mono_get_root_domain` 返回非 NULL）。
#: 而 UE 游戏**永远**不会有这两个模块，等满 60 秒纯属空转 ——
#: 实测 UE 样本每个 `launch_game` 都要白等 62 秒才如实返回。
#: 引擎在 `open_game` 时已经知道，没有理由再去猜。
#:
#: 历史订正：Mono 曾经用 2 秒预算，理由是「Mono 没有运行时探针，等也白等」。
#: 探针补上 Mono 后端之后这个理由不再成立 —— 现在等下去**真的会**就绪，
#: 给 2 秒只会让第一次运行时调用拿到「Mono 运行时尚未初始化」。
READY_TIMEOUT_MANAGED = 60.0
READY_TIMEOUT_OTHER = 2.0


def ready_timeout_for(engine: str | None) -> float:
    """按引擎决定 spawn 后的运行时就绪等待预算（纯函数，便于验收）。"""
    name = (engine or "").strip().lower()
    if name in ("il2cpp", "mono"):
        return READY_TIMEOUT_MANAGED
    return READY_TIMEOUT_OTHER


def runtime_label(engine: str | None) -> str:
    """运行时的可读名（提示语用）。

    为什么需要它：提示语曾经一律写死「IL2CPP 运行时未就绪」，
    于是在 Mono 游戏上也会这么说 —— 明明是 Mono 探针在等 Mono 域，
    却告诉 Agent 是 IL2CPP 的问题，属于「说了话但不准」。
    """
    name = (engine or "").strip().lower()
    if name == "il2cpp":
        return "IL2CPP"
    if name == "mono":
        return "Mono"
    return "托管运行时"


#: 判断「程序集加载完了」用的采样参数（Mono 专用）。
#: 0.5 秒采一次，连续 4 次数量不变就算稳定（≈2 秒静默窗口）。
ASSEMBLY_POLL_INTERVAL = 0.5
ASSEMBLY_STABLE_SAMPLES = 4
#: 等程序集稳定的总预算。实测 Archon Soul 从 13 涨到 164 用了约 2 秒；
#: 给 30 秒足够宽裕，又不至于让「这游戏本来就只有 3 个程序集」白等太久
#: （那种情况 2 秒静默窗口一到就返回）。
ASSEMBLY_SETTLE_BUDGET = 30.0


def _settle_assemblies(sess, engine: str | None, budget: float = ASSEMBLY_SETTLE_BUDGET) -> dict:
    """等 Mono 的懒加载程序集**加载稳定**再放行。

    为什么必须等（实测，不是理论）：注入后 0.5 秒时游戏只有 13 个程序集
    （`UnityEngine.*` 还没加载），而矩阵在此时就去 Hook 并强制调用
    `Time.get_frameCount` —— **Archon Soul 进程随后消失**。同一个游戏在
    程序集加载完（164 个）之后再走同样流程则一切正常。
    也就是说，「注入成功」不等于「可以 Hook 托管方法」。

    判据用「数量连续 N 次不变」而不是「数量 >= 某个阈值」：实测 Gambonanza
    在 4 秒时只有 15 个程序集（而 Archon Soul 有 164 个），阈值法会把它判成
    「永远没加载完」；而静默窗口对「程序集本来就少」的游戏会立刻返回。

    等不到稳定也不报错 —— 只如实说明等了多久、看到多少个，把判断留给调用方。
    """
    from .adapt import capabilities_of

    cap = capabilities_of(engine) if engine else {}
    if not cap.get("lazy_assemblies"):
        return {}
    t0 = time.time()
    last = -1
    same = 0
    seen: list[int] = []
    while time.time() - t0 < budget:
        try:
            n = int(sess.runtime_info().get("image_count") or 0)
        except Exception as exc:  # noqa: BLE001 - 探针挂了不该让注入失败
            return {"waited": round(time.time() - t0, 1),
                    "error": f"查询程序集数量失败：{exc}"}
        seen.append(n)
        if n == last:
            same += 1
            if same >= ASSEMBLY_STABLE_SAMPLES:
                return {"images": n, "waited": round(time.time() - t0, 1),
                        "samples": len(seen), "settled": True}
        else:
            same = 0
            last = n
        time.sleep(ASSEMBLY_POLL_INTERVAL)
    return {"images": last, "waited": round(time.time() - t0, 1),
            "samples": len(seen), "settled": False,
            "note": f"等了 {budget:.0f} 秒程序集仍在增加，未判定为稳定"}


def _with_settled_assemblies(sess, engine: str | None, init: dict) -> dict:
    """把「等程序集加载稳定」的结果并进 init，并记一条会话日志。

    抽出来是因为 spawn 与 attach 两条路径都要做同一件事；
    漏掉任何一条，那条路径上就会重现「Hook 把游戏打崩」的缺陷。
    """
    settled = _settle_assemblies(sess, engine)
    if not settled:
        return init
    _state.note(f"程序集加载稳定：{settled.get('images')} 个"
                f"（等了 {settled.get('waited')}s）")
    return {**init, "assemblies": settled}


def _is_under(path: str, root: str) -> bool:
    """`path` 是否落在 `root` 目录下（含 root 自身，容错大小写）。"""
    if not path or not root:
        return False
    try:
        a = os.path.normcase(os.path.abspath(path))
        b = os.path.normcase(os.path.abspath(root))
    except Exception:
        return False
    return a == b or a.startswith(b + os.sep)


def _build_index_for_context() -> dict:
    """按当前会话的引擎决定要不要建索引；永不抛，失败如实回报。

    刻意**不把索引失败当成整体失败**：调用方要的是「启动 / 注入这个进程」，
    索引只是一个附加的一致性收益（没有它，`search_*` 用不了但进程照跑）。
    但也不能假装成功 —— 失败原因原样放进返回值。
    """
    out: dict[str, Any] = {}
    engine = (_state.engine or "").lower()
    if engine not in ("il2cpp", "mono"):
        # UE / 未知引擎没有集中元数据可索引（open_game 也是这么处理的）
        out["index_skipped"] = (
            f"{_state.engine or '未知'} 引擎没有集中元数据，不建索引")
        return out
    try:
        res = ensure_index()
    except Exception as exc:            # noqa: BLE001 - 索引失败不该影响启动
        out["index_built"] = False
        out["index_error"] = f"建索引时异常：{exc}"
        return out
    if res.get("ok"):
        out["index_built"] = True
    else:
        out["index_built"] = False
        out["index_error"] = res.get("error", "建索引失败")
    return out


def align_context_with_exe(exe: str, build_index: bool = True) -> dict:
    """把会话上下文（游戏身份 + 索引）对齐到 `exe` 所属的游戏。

    为什么需要它（实测缺陷，不是推测）
    --------------------------------------------------
    `launch_game(exe=...)` / `inject_process(pid=...)` 允许指定**任意**目标，
    而会话里还带着「当前游戏是谁」（`game_dir` / `engine` / `metadata_path`）
    以及由它建立的索引。两者指向不同游戏时，工具不会报任何错，
    但索引与运行时已经错配 —— 实测表现是 `runtime_info` 报

        image_count=143, static_image_count=140, image_count_matches=false

    （运行时是刚启动的 ved，静态索引还是上一个会话的 Pass The Fear）。
    更危险的是落点：拿 A 游戏的静态 token 去对 B 游戏的运行时地址，
    对账必然失败，或更糟 —— 撞巧对上同一个 token 而挂错函数。
    按 AGENTS.md「不许假成功」，这种静默错配必须在源头消除。

    **不碰运行时**（重要）
    --------------------------------------------------
    本函数只调整「身份 + 索引」，绝不关闭当前 RuntimeSession：

      · 调用方（`ensure_runtime`）此刻持有的运行时，要么是刚连上/刚 spawn 出来的
        新会话（关掉就等于白注入），要么是它按「先连新的、再断旧的」语义
        自己负责关闭的旧会话。两条路径的所有权都在调用方，不在这里。
      · `select_game` 内部会调 `close_runtime()`，所以调用它之前必须把
        `_state.runtime` 摘下来，切换完再原样放回去。

    规则
    --------------------------------------------------
    · 能从 exe 反推出游戏目录、且与当前会话是同一个游戏 → 身份不动，
      仅在缺索引时补建（例如会话刚 open_game 过一个没有索引的 UE 游戏）。
    · 反推出来是**另一个游戏** → 切换身份并重建索引；返回 `switched=True`
      + `previous_game_dir`，调用方必须把这件事报给用户，不能偷偷换。
    · 反推不出来（exe 不在受支持的游戏目录里、或拿不到路径）→ **不猜**，
      `identified=False`，由调用方如实说明上下文可能不匹配。

    Args:
        exe:         目标可执行文件路径。
        build_index: 切换后是否建索引。默认 True（让会话自洽）。

    Returns:
        {ok, identified, switched, game_dir, engine, ...}
        `ok=False` 只在「确实要切换、但切换本身失败」时出现。
    """
    with _state.lock:
        try:
            root = discovery.game_root_for_exe(exe)
        except Exception as exc:        # noqa: BLE001 - 反推失败不该中断注入
            return {"ok": True, "identified": False, "switched": False,
                    "reason": f"从 exe 反推游戏目录失败：{exc}"}

        if not root:
            return {"ok": True, "identified": False, "switched": False,
                    "reason": "这个 exe 不在任何可识别的游戏目录里"}

        if _same_path(root, _state.game_dir):
            # 同一个游戏：身份不动，避免把已选好的会话推倒重来
            out: dict[str, Any] = {"ok": True, "identified": True,
                                   "switched": False, "same_game": True,
                                   "game_dir": _state.game_dir,
                                   "engine": _state.engine}
            if build_index and not _state.has_index():
                out.update(_build_index_for_context())
            return out

        previous = _state.game_dir
        # 摘掉运行时再 select_game：否则它会把「调用方正要用的那个会话」关掉。
        # （这是本函数唯一需要小心的地方，故在 docstring 里也写了一遍。）
        held_runtime = _state.runtime
        _state.runtime = None
        try:
            sel = select_game(game_dir=root)
        finally:
            _state.runtime = held_runtime

        if not sel.get("ok"):
            return {"ok": False, "identified": True, "switched": False,
                    "error": sel.get("error", f"切换到 {root} 失败")}

        # 用**实际**身份变化判断是否真的换了游戏：万一两个目录指向同一份
        # 元数据，select_game 会走「复用」分支而不改动身份。
        switched = not _same_path(_state.game_dir, previous)
        out = {"ok": True, "identified": True, "switched": switched,
               "previous_game_dir": previous,
               "game_dir": _state.game_dir, "engine": _state.engine}
        if build_index:
            out.update(_build_index_for_context())
        return out


def context_warning(exe: str, align: dict | None) -> str:
    """上下文可能不匹配时给出的一句人话说明；没问题时返回空串。

    只在「反推不出游戏目录」**且** exe 确实不在当前会话的游戏目录下时报警 ——
    否则会给正常的同游戏调用刷出无意义的警告。
    """
    if not align or align.get("identified"):
        return ""
    if not _state.game_dir or _is_under(exe, _state.game_dir):
        return ""
    return ("无法从这个 exe 反推出它属于哪个游戏，会话上下文"
            f"（{_state.game_dir}）可能与被注入的进程不是同一个游戏 —— "
            "索引与运行时的对账结果可能无意义。"
            "要更换上下文请显式调用 open_game。")


def _align_and_note(exe: str) -> dict:
    """对齐上下文并留痕（spawn / attach 两条路径共用）。

    抽出来的理由与 `_with_settled_assemblies` 相同：两条路径都要做同一件事，
    漏掉任何一条，那条路径上就会重现「索引与运行时错配」的缺陷。

    另外把「上下文可能不匹配」的提示一并算好放进 `context_warning`，
    免得每个调用点各自判断一遍（判断需要 exe，而 exe 只有这里最清楚）。
    """
    align = align_context_with_exe(exe)
    if align.get("switched"):
        _state.note(
            f"会话上下文已跟随目标切换：{align.get('previous_game_dir') or '(空)'} "
            f"→ {align.get('game_dir')}（引擎 {align.get('engine')}）")
    elif align.get("index_built"):
        _state.note(f"已按目标补齐索引（引擎 {_state.engine}）")
    text = context_warning(exe, align)
    if text:
        align = {**align, "context_warning": text}
        _state.note("警告：无法确认被注入的进程属于当前会话的游戏")
    return align


def ensure_runtime(
    pid: int = 0,
    auto_find: bool = True,
    spawn: bool = False,
    launcher_exe: str = "",
    preferred_module: str = "",
) -> dict:
    """保证当前游戏有已注入的运行时会话（必要时注入）。

    三种入口：
        pid         指定进程
        launcher_exe 指定可执行文件（会 spawn 并抢在启动前注入）
        auto_find   都不给时，按当前游戏的主程序名自动找进程
    """
    from ..runtime import (attach, spawn_and_attach, SessionError,
                           RuntimeUnavailable, probe_available)

    with _state.lock:
        #: 非 None 表示「新会话建好之后要断开的那个旧会话对象」。
        #:
        #: 为什么不在下面**立刻**断开旧会话（那是修复前的写法）：旧代码是
        #: 「先断开旧的，再去连新的」，于是**新目标连不上时旧会话已经没了**。
        #: 实测：`inject_process(pid=<不存在的 pid>)` 会先 detach 掉正常工作的
        #: 游戏会话，然后返回 `unable to find process with pid` —— 调用方只是想
        #: 试一个 pid，却把**正在用的会话弄没了**（后续所有 runtime_* 都报
        #: 「当前没有已注入的运行时会话」）。这是典型的「失败还带副作用」。
        #:
        #: 现在的规则：**先连新的，连上了才断旧的**；连不上就原样保留旧会话，
        #: 并在返回值里如实说明「旧会话仍在」。
        switching_from = None

        # ---------------------------------------------------------- 已有会话
        # 这里的判断很关键：早期实现是无条件 `if has_runtime(): return reused`，
        # 于是 inject_process(pid=B) 在已有 pid=A 的会话时**静默返回 A**，
        # launch_game() 也**永远不启动**（直接复用旧会话）。
        # 显式指定的目标被忽略，是比报错更糟的失败——Agent 以为成功了。
        #
        # 现在的规则：
        #   · 没指定目标            → 复用现有会话（这是「懒注入」的正常路径）
        #   · 指定的 pid 就是当前会话 → 复用
        #   · 指定了另一个 pid       → 切换：**连上新目标后**再断开旧的
        #   · 要求 spawn 新进程      → 拒绝并说明，绝不静默起第二个游戏进程
        if _state.has_runtime():
            cur = _state.runtime
            cur_pid = getattr(cur, "pid", 0)

            if not pid and not (launcher_exe and spawn):
                # 未指定目标：复用是正确的（Agent 通常只是想「确保有注入」）
                return {"ok": True, "reused": True, **_state.summary()}

            if launcher_exe and spawn:
                # 已经有一个活着的会话，再 spawn 会起**第二个游戏进程**，
                # 两个实例共享存档/配置，行为难以预料。拒绝，并给出出路。
                return {
                    "ok": False,
                    "error": f"已有一个运行中的会话（pid={cur_pid}"
                             f"{'，spawned' if getattr(cur, 'spawned', False) else ''}），"
                             "拒绝再启动新进程。",
                    "hint": "若确实要重新启动，先调用 close_session() 关闭当前会话，"
                            "再调用 launch_game()。"
                            "close_session 只卸载 Hook 并断开连接，不会结束游戏进程。",
                    "current_pid": cur_pid,
                }

            if pid and pid == cur_pid:
                return {"ok": True, "reused": True, **_state.summary()}

            # 指定了另一个 pid：**先只记下来**，等新会话真建好再断开旧的。
            # close() 只 detach（卸载 Hook、断开连接），**不会杀掉进程**，
            # 所以旧目标会恢复到未被修改的状态并继续运行。
            switching_from = cur

        # launcher_exe：spawn 模式
        if launcher_exe and spawn:
            try:
                sess = spawn_and_attach(
                    launcher_exe,
                    cwd=str(Path(launcher_exe).parent),
                    preferred_module=preferred_module,
                    # 由本函数统一等待，便于把「等了多久/试了几次」报给 Agent
                    init=False,
                )
            except (SessionError, RuntimeUnavailable) as exc:
                return {"ok": False, "error": str(exc)}
            _state.runtime = sess
            _state.note(f"已启动并注入 {Path(launcher_exe).name} (pid={sess.pid})")
            # spawn 的是**参数指定的 exe**，未必是会话当前选中的那个游戏。
            # 必须在这一步把上下文对齐过去，否则索引（旧游戏）与运行时（新游戏）
            # 会静默错配 —— 详见 align_context_with_exe。
            # 放在 wait_ready **之前**：就绪预算按引擎给，引擎得先是对的。
            align = _align_and_note(launcher_exe)
            if not align.get("ok"):
                # 切换失败：进程已经起来了，如实报告并把会话状态一并带出。
                return {"ok": False, "error": align.get("error", "会话上下文切换失败"),
                        "align": align, **_state.summary()}
            # spawn 路径：**第一次探测前先静默**（`wait_ready` 的默认 settle）。
            # 依据：对照实验里「游戏启动头几秒就去探测」会把卡死率从 0/8
            # 抬到 8/8（紧循环）；这是缓解措施，根因未定位，
            # 详见 `runtime/session.py` 的 `READY_SETTLE_DELAY`。
            init = sess.wait_ready(timeout=ready_timeout_for(_state.engine),
                                   preferred_module=preferred_module)
            if not init.get("ok"):
                # 启动并注入是成功的，只是托管运行时没就绪（可能不是 Unity 游戏）。
                # 如实报告，但仍算成功注入 —— 否则 Agent 会以为注入失败而重试。
                label = runtime_label(_state.engine)
                _state.note(f"{label} 运行时未就绪：{init.get('error')}")
                return {
                    "ok": True,
                    "warning": f"进程已启动并注入，但 {label} 运行时未就绪",
                    "init": init,
                    "align": align,
                    **_state.summary(),
                }
            # 「注入成功」不等于「可以 Hook 托管方法」：Mono 的程序集是懒加载的，
            # 此时去 Hook/调用会把游戏打崩（实测见 _settle_assemblies）。
            init = _with_settled_assemblies(sess, _state.engine, init)
            return {"ok": True, "init": init, "align": align, **_state.summary()}

        # 找 pid
        target_pid = pid
        if not target_pid:
            if not auto_find:
                return {"ok": False, "error": "未指定 pid 且 auto_find 为假"}
            exe_name = Path(_state.exe).name if _state.exe else ""
            cands = discovery.find_processes(only_games=False)
            # 优先按主程序名匹配
            match = None
            if exe_name:
                for p in cands:
                    if p.name.lower() == exe_name.lower():
                        match = p
                        break
            if match is None:
                # 退而求其次：找任意 IL2CPP/Mono 进程
                for p in cands:
                    if p.runtime in ("il2cpp", "mono"):
                        match = p
                        break
            if match is None:
                avail = probe_available()
                return {
                    "ok": False,
                    "error": "没有找到正在运行的目标游戏进程。"
                             "请先启动游戏，或显式传 pid。",
                    "expected_exe": exe_name,
                    "runtime_probe": avail,
                }
            target_pid = match.pid
            proc_name = match.name
        else:
            info = discovery.process_identity(target_pid)
            proc_name = info.get("name", "")

        # 目标进程的可执行文件路径：既用于给会话一个像样的名字，
        # 也用于把会话上下文对齐到它所属的游戏（见下方 _align_and_note）。
        # 顺带订正：这里以前读的是 `info.get("name")`，而 `process_identity`
        # 返回的键是 `path` —— 于是 `proc_name` 恒为空串，会话摘要里
        # 一直显示成「已注入 pid=17920 ()」。同一份数据换个键就读对了。
        target_exe = discovery.process_exe_path(target_pid)
        if target_exe:
            proc_name = Path(target_exe).name

        try:
            sess = attach(target_pid, name=proc_name,
                          preferred_module=preferred_module, init=False)
        except (SessionError, RuntimeUnavailable) as exc:
            # **连不上新目标时不许有副作用**：旧会话原样保留，并如实说明，
            # 免得调用方以为「只是这次失败」，其实已经把在用的会话弄丢了。
            out: dict = {"ok": False, "error": str(exc), "pid": target_pid}
            if switching_from is not None:
                old_pid = getattr(switching_from, "pid", 0)
                out["hint"] = (f"原有会话未受影响（仍是 pid={old_pid}），"
                               f"可以继续使用 runtime_* / hook_* 工具。")
                out["kept_pid"] = old_pid
            return out

        # 新会话已经连上 —— 此刻才断开旧会话（切换语义）。
        if switching_from is not None:
            old_pid = getattr(switching_from, "pid", 0)
            try:
                switching_from.close()
            except Exception as exc:      # noqa: BLE001 - 关旧会话失败不该毁掉新会话
                warn(f"断开旧会话 pid={old_pid} 失败：{exc}")
            _state.note(f"目标变更（pid {old_pid} → {target_pid}），"
                        f"已断开旧会话 pid={old_pid}（进程未被结束）")

        _state.runtime = sess
        _state.note(f"已注入 pid={target_pid} ({proc_name})")
        # 注入的目标未必是会话当前选中的那个游戏（显式 pid / 自动匹配都可能是
        # 另一个游戏）。必须把上下文对齐过去，否则索引与运行时会静默错配。
        # 放在 wait_ready **之前**：就绪预算按引擎给，引擎得先是对的。
        align = _align_and_note(target_exe) if target_exe else {
            "ok": True, "identified": False, "switched": False,
            "reason": "拿不到该进程的映像路径，无法反推游戏目录"}
        if not align.get("ok"):
            return {"ok": False, "pid": target_pid,
                    "error": align.get("error", "会话上下文切换失败"),
                    "align": align, **_state.summary()}
        # 进程已经在跑，模块本该就在；预算仍按引擎给。
        # `settle=0`：这条路径注入的是**已经启动好**的进程，
        # 没有「游戏启动头几秒」那个危险窗口 —— 白等 3 秒纯属浪费
        # （对照实验里的危险窗口只存在于 spawn 之后的那几秒）。
        init = sess.wait_ready(timeout=ready_timeout_for(_state.engine),
                               preferred_module=preferred_module, settle=0.0)
        if not init.get("ok"):
            label = runtime_label(_state.engine)
            _state.note(f"{label} 运行时未就绪：{init.get('error')}")
            return {
                "ok": True,
                "pid": target_pid,
                "warning": f"已注入，但 {label} 运行时未就绪"
                           "（非 Unity 托管游戏属正常）",
                "init": init,
                "align": align,
                **_state.summary(),
            }
        # 与 spawn 路径同理：进程已经在跑时程序集通常已经稳定，
        # 但「注入一个刚起来的 Mono 游戏」同样会碰到懒加载，所以一并等。
        init = _with_settled_assemblies(sess, _state.engine, init)
        return {"ok": True, "pid": target_pid, "init": init, "align": align,
                **_state.summary()}


# ---------------------------------------------------------------- 环境探测


def diagnostics(scan_processes: bool = False) -> dict:
    """汇总环境与依赖状态，供 Agent 判断能做什么、不能做什么。

    Args:
        scan_processes: 是否顺带扫本机正在运行的游戏。
            默认 **False** —— 该扫描实测耗时 2.66 秒（枚举 460 个进程），
            而它要回答的问题「有哪些游戏在跑」已经有专门的
            `find_game_processes` 工具。诊断工具不该每次调用都付这笔钱，
            也不该越界去做全机扫描（本 MCP 的运行上下文限定在游戏项目目录内）。
    """
    from ..runtime import probe_available, frida_version

    probes = probe_available()
    info: dict[str, Any] = {
        "python": {
            "version": ".".join(str(x) for x in __import__("sys").version_info[:3]),
            "executable": __import__("sys").executable,
        },
        "frida": {
            "installed": probes.get("available", False),
            "version": frida_version(),
            "detail": probes,
        },
        "engines": {
            "il2cpp": {
                "parser": "内置（纯标准库）",
                "ready": True,
                "note": "解析 global-metadata.dat，已在真实游戏上验证",
            },
            "mono": {
                "parser": "内置 ECMA-335 解析器",
                "ready": _mono_ready(),
                "note": "解析 Unity Mono 后端的 .NET 程序集",
            },
            "ue": {
                "parser": "内置（基于真实样本实测能力）",
                "ready": _ue_ready(),
                "note": "Unreal 的强项是资源容器解析，不是类型系统；"
                        "字段偏移/FName 池在静态样本中不可得",
                "capabilities": UE_CAPABILITIES,
            },
        },
        # 缓存目录与**真实占用**（P3-2）。以前只报路径，于是「缓存涨到几 GB」
        # 这件事在任何工具返回里都看不见。files/total_bytes 是现数现算的真数；
        # 清理边界（2 GiB / 最近 4 个 / 7 天）写在 index.py 常量里，不在这里重复。
        "cache": cache_stats_safe(),
        "session": _state.summary(),
    }

    # 反编译是**可选**能力（外部 ilspycmd，不是运行期依赖）。
    # 必须显式报告可用性：缺了它 Agent 应改走元数据索引，而不是反复试错。
    try:
        from . import decompile
        info["decompiler"] = decompile.available()
    except Exception as exc:
        info["decompiler"] = {
            "available": False, "tool": "", "version": "",
            "hint": f"反编译模块加载失败：{exc}",
        }

    # 弱视觉（截图 / OCR）同样是**可选**能力（dxcam|mss、rapidocr|tesseract）。
    # 与 decompiler 同一个道理：缺了要**现在就说**，否则 Agent 会反复试
    # capture_screen / ocr，每次拿回一个「没有可用后端」。
    try:
        from . import screen as _screen
        info["screen"] = _screen.available()
    except Exception as exc:
        info["screen"] = {
            "capture_usable": False, "ocr_usable": False,
            "hint": f"弱视觉模块加载失败：{exc}",
        }

    # 探测本机可用的游戏 —— 仅在显式要求时做（默认关闭，见 docstring）
    if scan_processes:
        try:
            all_procs = discovery.find_processes(only_games=True)
            info["running_games"] = [
                {"pid": p.pid, "name": p.name, "runtime": p.runtime}
                for p in all_procs[:20]
            ]
            info["running_game_count"] = len(all_procs)
        except Exception as exc:
            info["running_games"] = []
            info["running_games_error"] = str(exc)
    else:
        info["running_games_skipped"] = (
            "默认不扫本机进程（耗时约 2.7s）；要列运行中的游戏请用 "
            "find_game_processes，或传 scan_processes=True"
        )

    # 这里曾有一个 `optional_packages`：报 capstone / pefile / frida / mcp 装没装。
    # P3 复核后删除，两个理由：
    #   · `capstone` 与 `pefile` **本仓库从来不 import**（PE 解析是 pe.py 手写的，
    #     也没有指令级分析），报 `false` 会让 Agent 以为「装个包就能解锁能力」，
    #     而那个能力根本不存在 —— 这是典型的「说了话但不干活」的字段；
    #   · `frida` / `mcp` 是真的运行时依赖，且 `frida` 上面已经报过
    #     （`info["frida"]["installed"]`），`mcp` 恒为 true。

    return info


def cache_stats_safe() -> dict[str, Any]:
    """缓存目录的真实占用（P3-2）。

    以前这里是 `default_cache_dir_safe()`，**只返回一个路径字符串** ——
    于是「缓存涨到几 GB」这件事在任何工具的返回里都看不到，
    而缓存文件名带元数据摘要、游戏一更新就换一个文件，只会一直涨。

    统计失败不抛异常（诊断工具不该因为一个附加信息挂掉），
    但要**如实报 error**，不拿 0 冒充「缓存是空的」。
    """
    try:
        from .index import cache_stats
        return cache_stats()
    except Exception as exc:
        return {"dir": "", "files": 0, "total_bytes": 0,
                "error": f"缓存目录统计失败：{exc}"}


def _mono_ready() -> bool:
    try:
        from . import mono_meta  # noqa: F401
        return True
    except Exception:
        return False


def _ue_ready() -> bool:
    try:
        from . import ue  # noqa: F401
        return True
    except Exception:
        return False


# UE 的「能做什么」与 IL2CPP/Mono 差别很大，不能用一个布尔值概括：
# 资源容器解析已实测可用，但类型系统（FName 池、属性偏移、.usmap）
# 在静态样本里拿不到。把边界写清楚，免得 Agent 以为 UE 也能查字段偏移。
UE_CAPABILITIES = {
    "detect_game": "已实现：靠目录布局、打包清单、/Script/ 字符串识别",
    "pak_index": "已实现：.pak v11/v12 文件清单（实测与打包清单 100% 一致）",
    "iostore_index": "已实现：.utoc v8 目录索引（含 UTF-16 中文路径）",
    "manifest": "已实现：Manifest_*Files*.txt 解析",
    "script_objects": "已实现：exe 内 /Script/ 模块与对象路径（字符串证据）",
    "usmap": "未实现：样本中无 .usmap 文件，无可依据的真实格式",
    "fname_pool": "未实现：样本中无法定位 FName 池",
    "property_offsets": "未实现：需运行时读取，静态样本拿不到",
    "ue_version": "未实现：样本中无可信版本来源，返回空值而非猜测",
}


# ---------------------------------------------------------------- 静态分析辅助


def inspect_binary(path: str) -> dict:
    """分析一个可执行文件/DLL 的 PE 结构（不启动它）。"""
    p = Path(path)
    if not p.is_file():
        return {"ok": False, "error": f"文件不存在：{path}"}
    try:
        # 只解析一次。早先这里是 `info = pe_parse(p)` + `pe_summarize(p)`，
        # 而 summarize 内部又会 parse 一遍（pe.py:484）—— 于是同一个文件
        # 被完整解析两次，且 `info` **从未被使用**（实测白付 0.55 ms，
        # 约占该工具耗时的一半）。
        #
        # 去掉 `pe_parse` 不改变语义：文件非法时两者抛的都是同一个
        # PEError（summarize 走的就是 parse），下面照旧捕获。
        out = pe_summarize(p)
        return {"ok": True, "path": str(p), "pe": out}
    except Exception as exc:
        return {"ok": False, "error": f"PE 解析失败：{exc}", "path": str(p)}
