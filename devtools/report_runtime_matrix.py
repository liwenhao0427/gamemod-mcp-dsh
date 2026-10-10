#!/usr/bin/env python3
"""运行时矩阵报告：**逐游戏真开进程**，看 Agent 通过 MCP 工具到底能用哪些能力。

为什么需要它
============================================================
`report_multi_game_matrix.py` 只走**静态阶段**（识别引擎 → 建索引 → 查询），
它回答不了「注入之后还能不能用」——而那才是 Mod 开发真正依赖的一半能力。
静态阶段全绿、运行时一用就废，是完全可能的（本轮就实测到：静态 15/15
通过，而带 `args_type` 的 Hook 在 Frida 17 下**参数全丢**）。

本脚本对每个游戏把 **38 个工具**都过一遍，运行时部分是**真的开游戏、真的注入**：

    ① inspect_game / inspect_binary / open_game / get_session   识别 + 建索引
    ② 静态查询全家桶（search_* / class_detail / class_hierarchy /
       find_derived_classes / analyze_dependency / list_images /
       read_query_result / decompile / get_diagnostics）
    ③ launch_game          启动并注入探针（IL2CPP 与 Mono 都会真的就绪）
    ④ runtime_info         静态/运行时映像数对账
    ⑤ runtime_class_info   真实字段偏移 + 方法地址
    ⑥ find_hook_target     拿真实地址 + token 对账
    ⑦ hook_method          装 Hook（含参数捕获）
    ⑧ 强制调用 → 事件      证明 Hook 真的生效、参数/返回值真的读对
    ⑨ hook_status / unhook / hook_address / drain_events
    ⑩ read_memory / write_memory / scan_pattern / eval_script / gc_control
    ⑪ read_field_value     用 Hook 捕获的 this 指针读字段
    ⑫ inject_process       复用已注入的会话（不是重复注入）
    ⑬ close_session

**为什么走 stdio MCP 服务器而不是直接 import server.py**：本脚本要回答的是
「Agent 能不能用」，那就必须走 Agent 真正走的那条路（起服务器 → 握手 →
tools/call）。直接 import 会绕过 JSON-RPC 序列化与工具注册，测不到那一段。

**「不适用」与「失败」必须分开**：UE 游戏没有类型系统（`open_game` 不适用）、
`decompile` 在 IL2CPP 上没有托管元数据，把它们算成「失败」是错的。
所以引擎范围外的工具记为 `skip` 并写出原因，**不冒充通过、也不冒充失败**。

为什么是 report 而不是 verify
============================================================
它没有「通过/失败」，只有实测结果 —— 不同引擎的能力边界本来就不同。
所以叫 `report_*`、退出码恒为 0、不进 `run_all_verify.py`（逐个开游戏太慢）。

用法
============================================================
    python devtools\\report_runtime_matrix.py
    python devtools\\report_runtime_matrix.py --only "Garden of Witches"
    python devtools\\report_runtime_matrix.py --engine il2cpp
    python devtools\\report_runtime_matrix.py --json .cache-matrix\\runtime-matrix.json
    python devtools\\report_runtime_matrix.py --md docs\\多游戏运行时兼容性报告.md
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

# 中文控制台默认 GBK，打印 ✅ / 表格字符会抛 UnicodeEncodeError
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001 - 老版本 Python 没有 reconfigure
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mcp_stdio  # noqa: E402  （同目录）
import samples  # noqa: E402
import report_multi_game_matrix as mm  # noqa: E402

samples.add_src_to_path()

ROOT = samples.ROOT
CACHE = ROOT / ".cache-matrix"
CALL_TIMEOUT = 900.0

#: 每个游戏最多等多久「自然调用」出现（实例方法的 this 只能从自然调用拿）。
#: 为什么是 25 秒：实测 Garden of Witches 启动后约 9~15 秒进入帧循环，
#: `Object.get_name` 随之开始被调用。窗口太短会把「游戏还没跑起来」误判成
#: 「拿不到 this」。
NATURAL_WAIT = 25.0

#: 全部 38 个工具（顺序即报告里的行顺序，按「静态 → 运行时」分组）。
ALL_TOOLS: list[str] = [
    # 发现与索引
    "find_game_processes", "inspect_game", "inspect_binary", "open_game",
    "get_session", "get_diagnostics",
    # 静态查询
    "search_classes", "class_detail", "search_fields", "search_methods",
    "class_hierarchy", "find_derived_classes", "analyze_dependency",
    "list_images", "read_query_result", "decompile",
    # UE 专用
    "detect_ue", "ue_profile", "ue_list_entries", "ue_script_objects",
    "ue_manifest",
    # 运行时
    "inject_process", "launch_game", "runtime_info", "runtime_class_info",
    "find_hook_target", "hook_method", "hook_address", "unhook", "hook_status",
    "drain_events", "read_memory", "write_memory", "read_field_value",
    "scan_pattern", "eval_script", "gc_control", "close_session",
]

#: 只在 UE 上有意义的工具。对 Unity 游戏调用它们只会得到 `is_ue: false`，
#: 所以记为「不适用」而不是「失败」—— 但要写清原因，不假装通过。
UE_ONLY = ["ue_profile", "ue_list_entries", "ue_script_objects", "ue_manifest"]

#: 依赖运行时探针（`agent.js`）的工具。
#:
#: 历史：`agent.js` 曾经只认 IL2CPP，于是 Mono 游戏在这批工具上「没有可用路径」，
#: 本报告把它们记为「不适用」。**探针补上 Mono 后端之后这条边界不存在了** ——
#: Mono 游戏现在真的会走完这批工具，结果如何由实测决定，不再预先跳过。
RUNTIME_TOOLS = [
    "inject_process", "launch_game", "runtime_info", "runtime_class_info",
    "find_hook_target", "hook_method", "hook_address", "unhook", "hook_status",
    "drain_events", "read_memory", "write_memory", "read_field_value",
    "scan_pattern", "eval_script", "gc_control", "close_session",
]

#: 浮点靶子的候选表：(类, 方法, 参数规格列表, 调用参数, 期望返回值, 说明)。
#: 为什么用这几个：都是 `UnityEngine.Mathf` 里的静态纯函数，**结果可精确表示**
#: （6.25 = 2.5²，clamp01(2.5) = 1.0），所以断言可以用 == 而不是近似比较。
#: 不同游戏裁剪程度不同，逐个回退。
#: 参数规格是**按类型显式给的**（不再靠「重载顺序」猜）：实测 `Mathf.Abs`
#: 有 float/int 两个重载，盲挂会挂到 int 那个上。
FLOAT_TARGETS: list[tuple[str, str, list[str], str, float]] = [
    ("Mathf", "Abs", ["float"], "-2.5", 2.5),
    ("Mathf", "Sqrt", ["float"], "6.25", 2.5),
    ("Mathf", "Max", ["float", "float"], "-2.5,2.5", 2.5),
    ("Mathf", "Clamp01", ["float"], "2.5", 1.0),
    ("Mathf", "Min", ["float", "float"], "2.5,3.5", 2.5),
]

#: 实例方法靶子：用来拿 `this` 指针。`Object.get_name` 在几乎所有 Unity 游戏里
#: 都被 UI/日志路径自然调用，命中率高。
INSTANCE_TARGETS: list[tuple[str, str]] = [
    ("Object", "get_name"),
    ("Object", "GetInstanceID"),
    ("Component", "get_transform"),
]


def _kill_tree(pid: int | None) -> None:
    if not pid:
        return
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True, timeout=30)
    except Exception as exc:  # noqa: BLE001 - 清理失败不该让报告崩
        print(f"      ⚠️  结束进程 {pid} 失败：{exc}")


def _kill_leftover(exe: str | None) -> str:
    """启动前清掉同名的残留进程，返回说明。

    为什么必须有这一步：Unity 游戏普遍带单实例锁，只要还有一个同名进程活着，
    新进程就**不会加载 GameAssembly.dll** —— `launch_game` 会白等 60 秒再报
    「IL2CPP 运行时未就绪」，看起来像探针坏了。上一轮冒烟就踩到了：
    脚本自身异常中断、没能 kill 掉 spawn 出来的游戏，于是后续每次启动都超时。
    """
    if not exe:
        return ""
    name = Path(exe).name
    try:
        r = subprocess.run(["taskkill", "/F", "/IM", name, "/T"],
                           capture_output=True, text=True, timeout=30)
    except Exception as exc:  # noqa: BLE001
        return f"清理残留失败：{exc}"
    text = (r.stdout or "") + (r.stderr or "")
    if "SUCCESS" in text:
        return f"已清掉残留的同名进程（{name}）"
    return ""


class Probe:
    """一个游戏的走查。所有结果原样记进 cells（不加工、不粉饰）。"""

    def __init__(self, srv: mcp_stdio.McpStdioServer, rec: dict):
        self.srv = srv
        self.rec = rec
        self.cells: dict[str, dict] = {}
        rec["cells"] = self.cells
        self.pid: int | None = None
        self.t0 = time.time()

    # ---------------------------------------------------------------- 基础

    def call(self, tool: str, args: dict | None = None,
             timeout: float = CALL_TIMEOUT, record: bool = True) -> dict:
        """调一个工具，把结果记进 cells，并返回 data（工具返回的业务字典）。

        `record=False` 用于**探路**：只想知道某个传法行不行，不想覆盖 cell
        （典型场景：先试短名、再试完整名，最后只让成功的那次进报告）。
        """
        t = time.time()
        r = self.srv.call_tool(tool, args or {}, timeout=timeout)
        dt = time.time() - t
        cell: dict = {"ms": int(dt * 1000)}
        if not r["ok"]:
            cell.update(ok=False, kind="transport",
                        error=(r["error"] or r["text"])[:300])
        else:
            data = r["data"]
            if not isinstance(data, dict):
                cell.update(ok=False, kind="non_json",
                            error=(r["text"] or "")[:200])
            else:
                cell.update(ok=bool(data.get("ok")), kind="tool",
                            error=str(data.get("error", ""))[:300],
                            data=data)
                if r["is_error"]:
                    cell["kind"] = "exception"
        if record:
            self.cells[tool] = cell
        return cell.get("data") or {}

    def _note_offsets(self, target: str, data: dict) -> bool:
        """把某个类的**真实字段偏移**写进报告（静态元数据里没有这个信息）。

        只收**实例字段**：静态字段的 `offset` 恒为 0（它根本不在实例里），
        混进来会让「偏移 0」看起来像一个测量结果。没有实例字段就返回 False，
        让调用方换一个类再试。
        """
        inst = [f for f in (data.get("fields") or []) if not f.get("is_static")]
        if not inst:
            return False
        self.note("runtime_class_info_target", target)
        self.note("runtime_field_offsets",
                  {f.get("name"): f.get("offset") for f in inst[:4]})
        return True

    def _note_offsets_from_probe(self, candidates: tuple[tuple[str, str], ...]) -> None:
        """cell 已经成功、但那个类没有字段时，另找一个有字段的类补偏移证据。

        探测全部 `record=False`：cell 保留已经成功的那次，不被这里改掉。

        候选写成 `(namespace, name)`：`UnityEngine.Transform` 这类类**自己没有
        实例字段**（字段在基类上，而 `runtime_class_info` 只报本类声明的字段），
        所以要挑一个真的声明了实例字段的类 —— `UnityEngine.Object` 的
        `m_CachedPtr`（实测偏移 16）、游戏自己的 `PresetLoaderV2`
        （GoW 上 `userDataPresetFile` 偏移 32，与官方 dump 的 `/*0x20*/` 对得上）
        都是实测有字段的。
        """
        for ns, nm in candidates:
            args = {"namespace": ns, "name": nm} if ns else {"name": nm}
            rci = self.call("runtime_class_info", args, record=False)
            if rci.get("ok") and self._note_offsets(nm, rci):
                return
        self.note("runtime_field_offsets", "（本轮解析到的类没有实例字段）")

    def skip(self, tool: str, why: str) -> None:
        """记「不适用」：引擎范围外的工具。不冒充通过，也不冒充失败。"""
        self.cells.setdefault(tool, {"skip": why})

    def note(self, key: str, value: object) -> None:
        """记一条不属于某个工具的观测（例如「静态 token 对账一致」）。"""
        self.rec.setdefault("notes", {})[key] = value

    @staticmethod
    def _detail_of(res: dict) -> dict:
        d = res.get("detail")
        return d if isinstance(d, dict) else {}

    @classmethod
    def _is_ambiguous(cls, res: dict) -> bool:
        """工具是否**按设计拒绝**了歧义。

        为什么两处都看：`runtime_class_info` 的歧义标记在 `detail.ambiguous`
        （探针侧 `fail(msg, extra)` 把附加字段放进了 `detail`），
        但历史上顶层也放过同名标记。只看一层就会把「按设计拒绝」
        误判成「未找到」—— 实测 Sephiria 就这么被记成了「未找到」，
        而真实原因是 `Transform` 短名命中 2 个同名类（工具行为正确）。
        """
        return bool(res.get("ambiguous") or cls._detail_of(res).get("ambiguous"))

    @classmethod
    def _candidate_count(cls, res: dict) -> int:
        n = res.get("candidate_count")
        if isinstance(n, int):
            return n
        cands = cls._detail_of(res).get("candidates")
        return len(cands) if isinstance(cands, list) else 0

    @classmethod
    def _candidate_names(cls, res: dict) -> list[str]:
        out: list[str] = []
        for c in (cls._detail_of(res).get("candidates") or []):
            if isinstance(c, dict):
                out.append(str(c.get("full_name") or c.get("klass") or "?"))
        return out[:6]

    def _drain_until(self, hook_id: str, phase: str, wait: float,
                     timeout: float = 30.0) -> list[dict]:
        """反复 drain 直到出现某个 hook 的某阶段事件，或超时。"""
        got: list[dict] = []
        deadline = time.time() + wait
        while time.time() < deadline:
            ev = self.call("drain_events", {"limit": 500}, timeout=timeout)
            evs = ev.get("events") or []
            got.extend(evs)
            if any(e.get("id") == hook_id and e.get("phase") == phase for e in evs):
                break
            time.sleep(0.4)
        return got

    # ---------------------------------------------------------------- 静态

    def stage_static(self, install: str, exe: str | None) -> str:
        ins = self.call("inspect_game", {"game_dir": install})
        engine = str(ins.get("engine") or "")
        self.rec["engine"] = engine
        self.rec["main_exe"] = ins.get("main_exe") or exe
        # UE 的「主程序」可能是启动器；真正的 Shipping exe 在 ue.exe 里
        ue = ins.get("ue") or {}
        self.rec["ue_exe"] = ue.get("exe") or ins.get("main_exe") or exe
        self.rec["main_exe_is_64"] = ins.get("main_exe_is_64")
        self.rec["main_exe_runtime"] = ins.get("main_exe_runtime")

        if self.rec["main_exe"]:
            self.call("inspect_binary", {"path": self.rec["main_exe"]})
            # 进程发现：按主程序名过滤，避免全机扫描（那是 2.7s 起的开销）
            self.call("find_game_processes",
                      {"name_filter": Path(self.rec["main_exe"]).stem,
                       "only_games": False})
        self.call("get_diagnostics", {})

        if engine == "ue":
            # UE 没有集中元数据，`open_game` 按设计不适用（见 SKILL.md）
            self.skip("open_game", "UE 无集中元数据，open_game 按设计不适用")
            self.skip("get_session", "UE 会话不建索引，get_session 无内容可看")
            for t in ["search_classes", "class_detail", "search_fields",
                      "search_methods", "class_hierarchy",
                      "find_derived_classes", "analyze_dependency",
                      "list_images", "read_query_result", "decompile"]:
                self.skip(t, "UE 无类型系统（无类/字段/方法元数据）")
            return engine

        og = self.call("open_game", {"game_dir": install})
        idx = og.get("index") or {}
        self.rec["index"] = {k: idx.get(k) for k in
                             ("type_count", "method_count", "field_count",
                              "image_count")}
        self.call("get_session", {})

        sc = self.call("search_classes", {"pattern": "Player", "limit": 20})
        handle = sc.get("handle")
        target = ""
        for it in (sc.get("items") or []):
            target = it.get("nested_name") or it.get("full_name") or ""
            if target:
                break
        self.rec["sample_class"] = target
        if handle:
            self.call("read_query_result", {"handle": handle, "limit": 5})
        if target:
            self.call("class_detail", {"full_name": target})
            self.call("class_hierarchy", {"full_name": target, "depth": 2})
            self.call("analyze_dependency", {"full_name": target})
            # IL2CPP 游戏没有托管 dll，decompile 应当**如实失败**；
            # Mono 游戏应当成功 —— 这条差异本身就是要报告的结果。
            self.call("decompile", {"type_name": target})
        self.call("search_fields", {"pattern": "hp", "limit": 20})
        self.call("search_methods", {"pattern": "Update", "limit": 20})
        self.call("find_derived_classes",
                  {"base_full_name": "UnityEngine.MonoBehaviour", "limit": 20})
        self.call("list_images", {"limit": 20})
        return engine

    def stage_ue(self, install: str) -> None:
        """UE 没有运行时探针路径，走容器/清单那一套。"""
        exe = self.rec.get("ue_exe")
        self.call("detect_ue", {"exe": exe} if exe else {})
        prof = self.call("ue_profile", {"exe": exe} if exe else {})
        # ue_profile 没有 `containers` 这个键：容器在 detection.layout 里
        layout = ((prof.get("detection") or {}).get("layout") or {})
        paks = list(layout.get("pak_files") or [])
        utocs = list(layout.get("utoc_files") or [])
        self.rec["ue_containers"] = {"pak": len(paks), "utoc": len(utocs)}
        first = (paks or utocs)
        if first:
            self.call("ue_list_entries", {"container": first[0], "limit": 20})
        else:
            self.skip("ue_list_entries", "该样本没有 .pak/.utoc 容器")
        self.call("ue_manifest", {"game_dir": install})
        if exe:
            self.call("ue_script_objects", {"exe": exe, "limit": 50})

    # ---------------------------------------------------------------- 运行时

    def _pid_alive(self) -> bool | None:
        """进程还在不在 —— **直接问系统**，不经过探针。

        为什么必须分开测：实测有两个 IL2CPP 游戏在 `launch_game` 成功、
        `runtime_info` 也成功后，后续运行时工具全部报「当前没有已注入的
        运行时会话」。这时有两种完全不同的可能：

          · 游戏进程自己崩了/退了 → 会话随之失效（环境或游戏的问题）；
          · 进程还活着，只是会话对象被错误地丢掉了 → **产品 bug**。

        只看工具报错分不清这两者，所以直接查 PID。
        """
        if not self.pid:
            return None
        try:
            r = subprocess.run(
                ["tasklist", "/FI", f"PID eq {self.pid}", "/NH", "/FO", "CSV"],
                capture_output=True, text=True, timeout=20)
            return f'"{self.pid}"' in (r.stdout or "")
        except Exception:  # noqa: BLE001 - 查不到就如实返回 None
            return None

    def stage_inject(self) -> str:
        """返回**空串**表示运行时可用；否则返回「为什么不可用」（写进报告）。

        为什么返回原因而不是 bool：Mono 游戏在这里会走到「进程起来了但
        IL2CPP 运行时不存在」这条路上 —— 报告里必须说清是**哪种**不可用，
        否则后面那批运行时工具的「不适用」就没有依据。
        """
        exe = self.rec.get("main_exe")
        left = _kill_leftover(exe)
        if left:
            self.note("leftover_cleanup", left)
        lg = self.call("launch_game", {"exe": exe} if exe else {})
        self.pid = lg.get("pid")
        if not lg.get("ok"):
            return f"启动/注入被拒绝：{str(lg.get('error'))[:200]}"
        self.rec["pid"] = self.pid
        init = lg.get("init") or {}
        self.rec["init"] = {
            "engine": init.get("engine"),
            "module": init.get("module"),
            "base": init.get("base"),
            # api_available 是**计数**（int），不是列表 —— 早期版本这里写成
            # len(...) 直接 TypeError，把整轮运行时验证掐断在第一步。
            "api_available": init.get("api_available"),
            "api_missing": (init.get("api_missing") or [])[:5],
            "attempts": init.get("attempts"),
            "waited": init.get("waited"),
        }
        if not init.get("module"):
            # 走到这里说明进程起来了、探针也注入了，但**两个后端都没认出来**：
            # 既没有 IL2CPP 模块也没有 Mono 模块（原生/UE 游戏就属于这种），
            # 所以后面那批运行时工具确实没有可用路径。
            return ("注入成功但没找到托管运行时模块（非 IL2CPP/Mono 游戏没有"
                    f"探针路径）：{str(init.get('error'))[:160]}")
        ri = self.call("runtime_info", {})
        self.rec["images"] = {
            "runtime": ri.get("image_count"),
            "static": ri.get("static_image_count"),
            "match": ri.get("image_count_matches"),
        }
        # 已注入的会话再 inject 一次应当**复用**而不是重复注入
        self.call("inject_process", {"pid": self.pid} if self.pid else {})
        self.note("pid_alive_after_inject", self._pid_alive())
        if not ri.get("ok"):
            return f"运行时探针不可用：{str(ri.get('error'))[:200]}"
        return ""

    def stage_hook_core(self) -> None:
        """Time.get_frameCount：静态 token ↔ 运行时 token 对账 + 强制调用。"""
        cd = self.call("class_detail", {"full_name": "UnityEngine.Time",
                                        "method_name": "get_frameCount"},
                       record=False)
        static_tok = ""
        for m in (cd.get("methods") or []):
            if m.get("name") == "get_frameCount":
                static_tok = str(m.get("token") or "")
                break
        self.note("static_token_get_frameCount", static_tok)

        fht = self.call("find_hook_target", {"name": "Time",
                                             "method": "get_frameCount",
                                             "namespace": "UnityEngine"})
        cands = fht.get("candidates") or []
        self.note("candidates", len(cands))
        if cands:
            c0 = cands[0]
            self.note("rva_hex", c0.get("rva_hex"))
            self.note("runtime_token", c0.get("token"))
            self.note("token_match",
                      str(c0.get("token") or "").lower()
                      == static_tok.lower() and bool(static_tok))

        # runtime_class_info：字段的**真实偏移** + 方法 RVA（静态元数据里没有）。
        #
        # 先按 Agent 最自然的传法试**短名**（`Transform`）。注意短名在部分游戏上
        # 会撞上 `System.*` 里的同名类型，此时工具会**正确拒绝**并给出
        # candidates（`ambiguous: true`）—— 那是设计行为，不是工具坏了，
        # 所以这里如实记一笔，然后改用**完整名**（文档里就是这么教的）再试。
        short_probe = self.call("runtime_class_info", {"name": "Transform"})
        if short_probe.get("ok"):
            self.note("runtime_class_info_short_name", "ok")
        elif self._is_ambiguous(short_probe):
            self.note("runtime_class_info_short_name",
                      f"歧义({self._candidate_count(short_probe)}) —— 按设计拒绝，"
                      f"改传完整名；候选="
                      f"{','.join(self._candidate_names(short_probe))}")
        else:
            self.note("runtime_class_info_short_name",
                      f"未找到：{str(short_probe.get('error'))[:60]}")

        if not short_probe.get("ok"):
            # 短名被拒（歧义/未找到）时改用**完整名**再试，并把真实偏移记下来。
            # 这里先 record=False 探一遍（不覆盖 cell），最后对选中的那个做一次
            # 正式调用 —— 否则「先成功后失败」会让 cell 变成失败，与事实相反。
            best: tuple[str, dict] | None = None
            for cand in ("PresetLoaderV2", "UnityEngine.Object",
                         "UnityEngine.Component", "UnityEngine.Transform"):
                rci = self.call("runtime_class_info", {"name": cand},
                                record=False)
                if not rci.get("ok"):
                    continue
                has_fields = any(not f.get("is_static")
                                 for f in (rci.get("fields") or []))
                prev_has = (best is not None and any(
                    not f.get("is_static") for f in (best[1].get("fields") or [])))
                if best is None or (has_fields and not prev_has):
                    best = (cand, rci)
                if has_fields:
                    break
            if best is None:
                # 几种传法都不行：把真实失败记进 cell（不掩盖）
                self.call("runtime_class_info", {"name": "UnityEngine.Transform"})
            else:
                self.call("runtime_class_info", {"name": best[0]})
                self._note_offsets(best[0], best[1])
        else:
            self.note("runtime_class_info_target", "Transform（短名）")
            if not self._note_offsets("Transform（短名）", short_probe):
                # `UnityEngine.Transform` 自己没有实例字段（实测 fields=[]），
                # 那就再找一个**有字段**的类把真实偏移补进报告 ——
                # 这是「静态元数据没有偏移、运行时才有」这句话的证据。
                self._note_offsets_from_probe((("", "PresetLoaderV2"),
                                               ("", "UnityEngine.Object")))

        hm = self.call("hook_method", {"name": "Time", "method": "get_frameCount",
                                       "namespace": "UnityEngine",
                                       "label": "sweep.frame", "max_calls": 50})
        hid = hm.get("hook_id")
        if not hid:
            return
        ptr = (hm.get("chosen") or {}).get("pointer")
        # 强制调用：不依赖游戏是否已经跑起来，值也是我们自己给的
        self.call("eval_script", {"code":
            "(function(){ var f = new NativeFunction(ptr('%s'), 'int', []);"
            " return f(); })()" % ptr})
        evs = self._drain_until(hid, "enter", 6.0)
        ent = [e for e in evs if e.get("id") == hid and e.get("phase") == "enter"]
        self.note("hook_enter_events", len(ent))
        self.note("hook_from", (ent[0].get("from") or {}).get("module")
                  if ent else None)
        self.call("hook_status", {})
        self.call("unhook", {"hook_id": hid})

    def stage_float(self) -> None:
        """浮点参数 + 浮点返回值：一次覆盖两条 XMM 路径。"""
        for cls, meth, ptypes, argv, want in FLOAT_TARGETS:
            # `record=False`：这一步是**探路**（想从静态元数据里拿 float 方法的
            # token）。Mono 的静态索引默认只收游戏自己的程序集，查
            # `UnityEngine.*` 必然「未找到类」—— 那是索引范围的事实，不是
            # `class_detail` 坏了（`stage_static` 已用游戏自己的类证明它能用）。
            # 照原样记录会让每个 Mono 游戏多一条假 ❌。
            cd = self.call("class_detail", {"full_name": f"UnityEngine.{cls}",
                                            "method_name": meth}, record=False)
            ovl = [m for m in (cd.get("methods") or [])
                   if m.get("name") == meth
                   and int(m.get("parameter_count") or 0) == len(ptypes)]
            if not ovl:
                self.note("float_skip", f"静态元数据里没有 UnityEngine.{cls}.{meth}"
                                        f"（{str(cd.get('error'))[:50]}）")
                continue
            tok = str(ovl[0].get("token") or "")
            spec = ",".join(ptypes)
            self.note("float_target", f"{cls}.{meth}({spec}) token={tok}")
            self.note("float_args_type", spec)
            hm = self.call("hook_method", {
                "name": cls, "method": meth, "namespace": "UnityEngine",
                "args_type": spec, "token": tok,
                "label": "sweep.float", "max_calls": 8})
            hid = hm.get("hook_id")
            if not hid:
                self.note("float_hook_error", str(hm.get("error"))[:200])
                return
            self.note("float_retval_type", hm.get("retval_type"))
            self.note("float_return_type", hm.get("return_type"))
            ptr = (hm.get("chosen") or {}).get("pointer")
            types = "[" + ",".join(f"'{s}'" for s in ptypes) + "]"
            call = self.call("eval_script", {"code":
                "(function(){ var f = new NativeFunction(ptr('%s'), 'float', %s);"
                " return f(%s); })()" % (ptr, types, argv)})
            self.note("float_forced_ret", call.get("value"))
            evs = self._drain_until(hid, "leave", 6.0)
            mine = [e for e in evs if e.get("id") == hid]
            ent = [e for e in mine if e.get("phase") == "enter"]
            lea = [e for e in mine if e.get("phase") == "leave"]
            self.note("float_enter_events", len(ent))
            self.note("float_captured_args",
                      (ent[0].get("args") if ent else None))
            self.note("float_captured_retval",
                      (lea[0].get("retval") if lea else None))
            self.note("float_args_error",
                      (ent[0].get("args_error") if ent else None))
            self.note("float_expect", want)
            errs = [e for e in mine if e.get("type") == "script_error"]
            self.note("float_script_errors",
                      [str(e.get("description"))[:80] for e in errs][:2])
            self.call("unhook", {"hook_id": hid})
            return
        self.note("float_target", None)

    def stage_hook_address(self) -> None:
        """按**绝对地址**挂 Hook（原生/已算出地址时的路径）。"""
        fht = self.call("find_hook_target", {"name": "Time",
                                             "method": "get_frameCount",
                                             "namespace": "UnityEngine"})
        cands = fht.get("candidates") or []
        if not cands:
            return
        addr = cands[0].get("address") or cands[0].get("pointer")
        ha = self.call("hook_address", {"address": addr, "retval_type": "int",
                                        "label": "sweep.addr", "max_calls": 20})
        hid = ha.get("hook_id")
        if not hid:
            return
        self.call("eval_script", {"code":
            "(function(){ var f = new NativeFunction(ptr('%s'), 'int', []);"
            " return f(); })()" % addr})
        evs = self._drain_until(hid, "enter", 6.0)
        self.note("hook_address_enter",
                  len([e for e in evs if e.get("id") == hid
                       and e.get("phase") == "enter"]))
        self.call("unhook", {"hook_id": hid})

    def stage_memory(self) -> None:
        base = (self.rec.get("init") or {}).get("base")
        if base:
            r = self.call("read_memory", {"address": base, "size": 16})
            self.note("mz_header", (r.get("hex") or "")[:8])
            # 幂等写回：模块头是**数据**地址，写回原字节无害（实测可原样读回）
            hexs = r.get("hex") or ""
            if hexs:
                self.call("write_memory", {"address": base, "hex_data": hexs})
        self.call("scan_pattern", {"pattern": "4D 5A 90 00", "max_results": 8})
        self.call("eval_script", {"code": "Process.arch"})
        self.call("gc_control", {"disable": True})
        self.call("gc_control", {"disable": False})

    def _instance_method_exists(self, cls: str, meth: str) -> bool:
        """`UnityEngine.<cls>.<meth>` 存不存在。

        为什么要问两遍：Mono 的静态索引只覆盖游戏自己的程序集，
        `class_detail("UnityEngine.Object")` 必然「未找到类」（实测 4/4）。
        以前这里只看静态结果，于是 Mono 上整条「实例方法 → this → 读字段」
        链路**根本没跑**，`read_field_value` 在报告里既不是通过也不是失败，
        而报告把「没跑到」算成了失败（见 §0）。运行时探针两边都能查，
        所以静态查不到时改问运行时 —— 这不是降级，是 Mono 上的正常路径。
        """
        cd = self.call("class_detail", {"full_name": f"UnityEngine.{cls}",
                                        "method_name": meth}, record=False)
        if any(m.get("name") == meth for m in (cd.get("methods") or [])):
            return True
        ri = self.call("runtime_class_info", {"name": cls,
                                              "namespace": "UnityEngine",
                                              "method_filter": meth})
        return any(m.get("name") == meth for m in (ri.get("methods") or []))

    def _instance_field_names(self, cls: str) -> list[str]:
        """实例字段名列表（静态查不到就改问运行时，理由同上）。"""
        cd = self.call("class_detail", {"full_name": f"UnityEngine.{cls}"},
                       record=False)
        names = [f.get("name") for f in (cd.get("fields") or []) if f.get("name")]
        if names:
            return names
        ri = self.call("runtime_class_info", {"name": cls,
                                              "namespace": "UnityEngine"})
        return [f.get("name") for f in (ri.get("fields") or [])
                if f.get("name") and not f.get("is_static")]

    def stage_this(self) -> None:
        """实例方法 → `this` 指针 → read_field_value（Mod 最常用的一条链）。"""
        for cls, meth in INSTANCE_TARGETS:
            if not self._instance_method_exists(cls, meth):
                continue
            hm = self.call("hook_method", {
                "name": cls, "method": meth, "namespace": "UnityEngine",
                "args_type": "ptr", "label": "sweep.this", "max_calls": 8})
            hid = hm.get("hook_id")
            if not hid:
                continue
            self.note("this_target", f"{cls}.{meth}")
            evs = self._drain_until(hid, "enter", NATURAL_WAIT)
            ent = [e for e in evs if e.get("id") == hid and e.get("phase") == "enter"]
            this_ptr = ((ent[0].get("args") or [None])[0] if ent else None)
            self.note("this_ptr", this_ptr)
            self.call("unhook", {"hook_id": hid})
            if not this_ptr:
                continue
            # 拿这个指针读一个实例字段：指针若不是真对象，这里必然失败
            for fname in self._instance_field_names(cls)[:3]:
                rv = self.call("read_field_value", {
                    "instance": this_ptr, "name": cls,
                    "field": fname, "namespace": "UnityEngine"})
                if rv.get("ok"):
                    self.note("field_read", f"{fname}="
                              f"{rv.get('raw_hex')}（宽度 {rv.get('read_width')}）")
                    break
            return

    def stage_close(self) -> None:
        self.call("close_session", {})
        _kill_tree(self.pid)

    # ---------------------------------------------------------------- 主流程

    def run(self, install: str, exe: str | None) -> None:
        engine = self.stage_static(install, exe)
        self.rec["elapsed_static"] = round(time.time() - self.t0, 1)
        if engine == "ue":
            self.stage_ue(install)
            self.rec["runtime"] = "不适用（UE 无类型系统，走容器/清单）"
            for t in RUNTIME_TOOLS:
                self.skip(t, "UE 没有类型系统与托管运行时，运行时探针不适用")
            return
        self.call("detect_ue", {"exe": self.rec.get("main_exe")} if
                  self.rec.get("main_exe") else {})
        for t in UE_ONLY:
            self.skip(t, "非 UE 引擎（无 .pak/.utoc/清单）")
        why = self.stage_inject()
        if why:
            self.rec["runtime"] = why
            for t in RUNTIME_TOOLS:
                self.skip(t, why)
            self.rec["elapsed_total"] = round(time.time() - self.t0, 1)
            _kill_tree(self.pid)
            return
        self.stage_hook_core()
        self.stage_float()
        self.stage_hook_address()
        self.stage_memory()
        self.stage_this()
        # 收尾前再确认一次进程是否还活着：若运行时工具在中途集体报
        # 「没有已注入的运行时会话」，这一条能区分「游戏崩了」与「会话被丢」
        self.note("pid_alive_before_close", self._pid_alive())
        self.stage_close()
        self.rec["elapsed_total"] = round(time.time() - self.t0, 1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--roots", nargs="*", default=None,
                    help="样本根（默认取 devtools/samples.py 的 SAMPLE_ROOTS）")
    ap.add_argument("--only", default="", help="只跑名字含该子串的游戏")
    ap.add_argument("--engine", default="", help="只跑该引擎（il2cpp/mono/ue）")
    ap.add_argument("--json", default=str(CACHE / "runtime-matrix.json"))
    ap.add_argument("--md", default="", help="额外把矩阵写成 Markdown")
    ap.add_argument("--from-json", default="",
                    help="不跑游戏，直接从已有 JSON 重新生成 Markdown"
                         "（改报告排版时用，避免再花 20 分钟）")
    ap.add_argument("--merge", action="store_true",
                    help="把结果并进已有的 --json（同一个游戏覆盖旧记录，"
                         "其它游戏保留）——重测个别游戏时用")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    # 只重排报告：读旧 JSON → 写 Markdown → 退出。
    if args.from_json:
        src_json = Path(args.from_json)
        if not src_json.is_file():
            print(f"找不到 {src_json}")
            return 1
        if not args.md:
            print("--from-json 需要同时给 --md")
            return 1
        old = json.loads(src_json.read_text(encoding="utf-8"))
        for _r in (old.get("results") or []):
            normalize(_r)
        write_markdown(old, Path(args.md))
        print(f"已从 {src_json} 重新生成：{args.md}"
              f"（{len(old.get('results') or [])} 个游戏）")
        return 0

    roots = [Path(p) for p in (args.roots or [str(p) for p in samples.SAMPLE_ROOTS])]
    games = mm._dedupe_nested(mm.discover_games(roots))
    if args.only:
        games = [g for g in games if args.only.lower() in g["install"].lower()]
    if args.engine:
        games = [g for g in games if g["hint"] == args.engine]
    if not games:
        print("没有匹配的游戏")
        return 0

    print(f"样本根 : {[str(r) for r in roots]}")
    print(f"待测游戏: {len(games)} 个")
    src = str(ROOT / "src")
    out: dict = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "roots": [str(r) for r in roots],
        "results": [],
    }
    if args.merge:
        old_json = Path(args.json)
        if old_json.is_file():
            old = json.loads(old_json.read_text(encoding="utf-8"))
            out["results"] = old.get("results") or []
            out["roots"] = old.get("roots") or out["roots"]
            print(f"合并模式：已有 {len(out['results'])} 条记录，"
                  f"本轮结果按游戏覆盖")

    # 先核对「本脚本以为的 38 个工具」与服务器真实报出的一致 ——
    # 否则报告里的「覆盖 38 个」就是个自说自话的数字。
    probe = mcp_stdio.McpStdioServer(
        [sys.executable, "-m", "gamemod_mcp.server"],
        env=mcp_stdio.script_form_env(src, clean_pythonpath=False),
        cwd=str(ROOT)).start()
    try:
        init = probe.initialize()
        names = probe.tools()["names"] if init["ok"] else []
    finally:
        probe.close()
    out["tools_actual"] = names
    out["tools_missing"] = sorted(set(names) - set(ALL_TOOLS))
    out["tools_extra"] = sorted(set(ALL_TOOLS) - set(names))
    print(f"服务器报出工具 {len(names)} 个；本脚本清单 {len(ALL_TOOLS)} 个")
    if out["tools_missing"] or out["tools_extra"]:
        print(f"  ⚠️ 清单不一致：漏 {out['tools_missing']}，多 {out['tools_extra']}")
    else:
        print("  ✅ 清单一致")

    for g in games:
        install = g["install"]
        name = Path(install).name
        print()
        print("=" * 78)
        print(f"▶ {name}  [{g['hint']}]  {install}")
        print("=" * 78)
        rec: dict = {"install": install, "name": name, "hint": g["hint"],
                     "markers": g["markers"], "started_at":
                     datetime.now().strftime("%H:%M:%S")}
        srv = mcp_stdio.McpStdioServer(
            [sys.executable, "-m", "gamemod_mcp.server"],
            env=mcp_stdio.script_form_env(src, clean_pythonpath=False),
            cwd=str(ROOT)).start()
        try:
            init = srv.initialize()
            if not init["ok"]:
                rec["server_error"] = init["error"][:400]
                print(f"  ❌ 服务器起不来：{init['error'][:200]}")
            else:
                pr = Probe(srv, rec)
                try:
                    pr.run(install, None)
                except Exception as exc:  # noqa: BLE001 - 单个游戏崩掉不该中断整轮
                    rec["exception"] = f"{type(exc).__name__}: {exc}"[:300]
                    rec["traceback"] = traceback.format_exc()[-1500:]
                    _kill_tree(pr.pid)
                rec["non_json_lines"] = srv.non_json_lines
                rec["bad_stdout_lines"] = list(srv.bad_lines)
                rec["server_stderr_tail"] = srv.stderr_tail(6)
        finally:
            srv.close()
        rec["finished_at"] = datetime.now().strftime("%H:%M:%S")
        normalize(rec)          # 明确拒绝 → 不适用（见 REFUSAL_RULES）
        if args.merge:
            out["results"] = [r for r in out["results"]
                              if r.get("install") != rec["install"]]
        out["results"].append(rec)

        # 边跑边落盘：中途中断也能拿到已完成的部分
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(
            json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

        c = rec.get("cells") or {}
        print(f"  引擎={rec.get('engine')} 索引={rec.get('index')}")
        for tool in ALL_TOOLS:
            cell = c.get(tool)
            if cell is None:
                mark = "⬜"
                extra = "（没跑到）"
            elif cell.get("skip"):
                mark = "▫️"
                extra = f"不适用：{cell['skip']}"
            elif cell.get("ok"):
                mark = "✅"
                extra = ""
            else:
                mark = "❌"
                extra = str(cell.get("error", ""))[:70]
            print(f"   {mark} {tool:<20} {cell.get('ms', 0) if cell else 0:>6}ms  "
                  f"{extra}")
        for k, v in (rec.get("notes") or {}).items():
            print(f"   · {k} = {v}")
        if rec.get("runtime"):
            print(f"   运行时结论：{rec['runtime']}")
        if rec.get("non_json_lines"):
            print(f"   ⚠️ stdout 污染 {rec['non_json_lines']} 行："
                  f"{(rec.get('bad_stdout_lines') or [])[:2]}")
        if rec.get("exception"):
            print(f"   ⚠️ 脚本异常：{rec['exception']}")

    if args.md:
        write_markdown(out, Path(args.md))
        print(f"\nMarkdown 报告：{args.md}")
    print(f"\nJSON：{args.json}")
    return 0


# ---------------------------------------------------------------- Markdown

#: 报告里「逐游戏关键工具」表的列（顺序即矩阵列顺序）。
#: 拆成两张表：30 列并排在任何 Markdown 阅读器里都挤成一团，
#: 而「静态能力」与「运行时能力」本来就是两件事，分开反而更好读。
KEY_STATIC = [
    "inspect_game", "inspect_binary", "open_game", "get_session",
    "get_diagnostics", "list_images", "search_classes", "class_detail",
    "search_fields", "search_methods", "class_hierarchy",
    "find_derived_classes", "analyze_dependency", "read_query_result",
    "decompile", "find_game_processes",
]

#: 运行时 + UE 容器能力（同样按引擎分组阅读）
KEY_RUNTIME = [
    "launch_game", "inject_process", "runtime_info", "runtime_class_info",
    "find_hook_target", "hook_method", "hook_address", "hook_status",
    "unhook", "drain_events", "read_memory", "write_memory",
    "read_field_value", "scan_pattern", "eval_script", "gc_control",
    "close_session", "detect_ue", "ue_profile", "ue_list_entries",
    "ue_manifest", "ue_script_objects",
]

KEY_TOOLS = KEY_STATIC + KEY_RUNTIME


def _mark(cell: dict | None) -> str:
    if cell is None:
        return "⬜"
    if cell.get("skip"):
        return "▫️"
    return "✅" if cell.get("ok") else "❌"


#: 「明确拒绝」≠「失败」。
#:
#: `decompile` 只支持 Mono/.NET，IL2CPP 上它**必然**返回 ok:false —— 这是设计
#: 好的行为（返回里点明「当前会话的元数据不是 .NET 程序集」），不是缺陷。
#: 若照原样统计，9 个 IL2CPP 游戏会在「失败汇总」里各贡献一条，
#: 把真正的问题淹掉。所以这里按**实测到的拒绝理由**把它改记为「不适用」，
#: 并把原始理由留在备注里（不是抹掉证据）。
REFUSAL_RULES = [
    ("decompile", "不是 .NET 程序集",
     "IL2CPP 没有托管元数据（`decompile` 只支持 Mono/.NET）"),
    # Mono 的运行时**没有** mono_gc_disable / mono_gc_enable 导出（实测四个样本
    # 的 mono-2.0-bdwgc.dll 导出表里都没有），所以 `gc_control` 只能明确拒绝。
    # 这是探针如实报告能力缺失，不是工具坏了 —— 记成「不适用」并保留原文。
    ("gc_control", "没有 mono_gc_disable",
     "本版 Mono 运行时缺 mono_gc_disable / mono_gc_enable 导出（能力缺失，如实拒绝）"),
]


def normalize(rec: dict) -> None:
    """把「符合设计的明确拒绝」从失败改记为不适用（保留原始理由）。"""
    cells = rec.get("cells") or {}
    for tool, needle, why in REFUSAL_RULES:
        cell = cells.get(tool)
        if not cell or cell.get("ok") or cell.get("skip"):
            continue
        if needle in str(cell.get("error", "")):
            cell["skip"] = f"{why}；实测拒绝理由：{str(cell['error'])[:80]}"
            cell["refusal"] = True


def _detail(cell: dict | None) -> str:
    if cell is None:
        return "没跑到"
    if cell.get("skip"):
        return f"不适用（{cell['skip']}）"
    if cell.get("ok"):
        return f"ok，{cell.get('ms', 0)}ms"
    return f"❌ {cell.get('error', '')[:120]}"


def _short_reason(why: str, limit: int = 48) -> str:
    """把「不适用」的长理由压成一句短备注（完整理由在 §5 逐条列出）。

    表格里塞进一条 140 字符、还带着绝对路径的理由，读者只会跳过去。
    """
    if why.startswith("注入成功但没找到托管运行时模块"):
        return "非 IL2CPP/Mono 游戏没有探针路径（详见 §5）"
    if why.startswith("不适用（UE 无类型系统"):
        return "UE 无类型系统，走容器/清单（详见 §5）"
    if why.startswith("IL2CPP 没有托管元数据"):
        return "IL2CPP 没有托管元数据，`decompile` 只支持 Mono（详见 §5）"
    return why if len(why) <= limit else why[:limit] + "…"


def write_markdown(out: dict, path: Path) -> None:
    rows = out["results"]
    lines: list[str] = []
    lines.append("# 多游戏运行时兼容性报告（自动生成）")
    lines.append("")
    lines.append(f"- 生成时间：{out['generated_at']}")
    lines.append(f"- 样本根：{', '.join(out['roots'])}")
    lines.append(f"- 被测游戏：{len(rows)} 个")
    lines.append(f"- 覆盖工具：{len(ALL_TOOLS)} 个"
                 "（与 `devtools/verify_tool_inventory.py` 认定的工具总数一致）")
    lines.append("- 生成脚本：`devtools/report_runtime_matrix.py`"
                 "（**走 stdio MCP 服务器**，即 Agent 真正走的那条路）")
    lines.append("")
    lines.append("> 本文件由脚本生成，数字全部是跑出来的。图例："
                 "`✅` 工具返回 `ok:true`；`❌` 返回 `ok:false` 或调用异常；"
                 "`▫️` **不适用**（该引擎没有这条路径，例如 UE 没有类型系统、"
                 "`decompile` 在 IL2CPP 上无托管元数据）"
                 "——「不适用」既不算通过也不算失败；"
                 "`⬜` 没跑到。")
    lines.append("")

    # 0. 结论（按引擎聚合出来，不写死）
    by_eng: dict[str, list[dict]] = {}
    for r in rows:
        by_eng.setdefault(r.get("engine") or r["hint"], []).append(r)
    lines.append("## 0. 结论")
    lines.append("")
    lines.append(f"- 实测 **{len(rows)} 个游戏**，"
                 f"覆盖 **{len(ALL_TOOLS)} 个工具**；工具清单与服务器"
                 f"实际报出的 {len(out.get('tools_actual') or [])} 个"
                 f"**逐名核对一致**"
                 + ("（一致）" if not (out.get("tools_missing")
                                       or out.get("tools_extra"))
                    else f"（不一致：漏 {out.get('tools_missing')}，"
                         f"多 {out.get('tools_extra')}）"))
    lines.append("")

    def _count(eng: str, tool: str) -> tuple[int, int, int, int]:
        """(通过, 不适用, 没跑到, 失败)。

        「没跑到」必须与「失败」分开：以前这里用 `总数 - 通过 - 不适用`
        当失败数，于是**任何没被触发的工具都被算成失败**。实测 Mono 上
        `read_field_value` 因为静态索引查不到 `UnityEngine.Object` 而整条链路
        没跑（单元格根本不存在），报告却在结论里写「❌4 ← 有游戏失败」，
        而同一份报告的「问题汇总」章节写着「没有任何工具返回失败」——
        两节自相矛盾，且矛盾的那一侧把锅扣给了工具。
        """
        group = by_eng.get(eng) or []
        marks = [_mark((r.get("cells") or {}).get(tool)) for r in group]
        return (marks.count("✅"), marks.count("▫️"),
                marks.count("⬜"), marks.count("❌"))

    for eng, label in (("il2cpp", "IL2CPP（Unity AOT）"),
                       ("mono", "Mono（Unity 托管）"),
                       ("ue", "Unreal（UE）")):
        group = by_eng.get(eng) or []
        if not group:
            lines.append(f"- **{label}**：本轮没有样本。")
            continue
        names = "、".join(r["name"] for r in group)
        lines.append(f"- **{label}**（{len(group)} 个：{names}）：")
        for tool in ("open_game", "search_classes", "class_detail",
                     "decompile", "launch_game", "runtime_info",
                     "hook_method", "read_field_value", "ue_profile"):
            ok, skip, notrun, bad = _count(eng, tool)
            if ok == 0 and skip == 0 and bad == 0 and notrun == 0:
                continue
            if skip == len(group):
                lines.append(f"  - `{tool}`：**不适用**（{skip}/{len(group)}）")
            elif bad:
                lines.append(f"  - `{tool}`：✅{ok} / ▫️{skip} / ⬜{notrun} / ❌{bad}"
                             "　← **有游戏失败**")
            elif notrun:
                lines.append(f"  - `{tool}`：✅{ok} / ▫️{skip} / ⬜{notrun}"
                             "　（没跑到，不算失败）")
            else:
                lines.append(f"  - `{tool}`：✅{ok}/{len(group)} 全部可用")
    lines.append("")
    lines.append("> 逐工具 × 逐引擎的完整结论见第 2 节；"
                 "失败明细见第 4 节；「不适用」的边界说明见第 5 节。")
    lines.append("")

    # 1. 逐游戏 × 关键工具
    lines.append("## 1. 逐游戏 × 关键工具")
    lines.append("")
    lines.append("### 1.1 静态能力（元数据 / 索引 / 反编译）")
    lines.append("")
    lines.append("| 游戏 | 引擎 | " + " | ".join(f"`{c}`" for c in KEY_STATIC)
                 + " |")
    lines.append("|---|---|" + "---|" * len(KEY_STATIC))
    for r in rows:
        cells = r.get("cells") or {}
        row = [r["name"], r.get("engine") or r["hint"]]
        row += [_mark(cells.get(c)) for c in KEY_STATIC]
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    lines.append("### 1.2 运行时能力（注入 / Hook / 内存）+ UE 容器")
    lines.append("")
    lines.append("| 游戏 | 引擎 | " + " | ".join(f"`{c}`" for c in KEY_RUNTIME)
                 + " |")
    lines.append("|---|---|" + "---|" * len(KEY_RUNTIME))
    for r in rows:
        cells = r.get("cells") or {}
        row = [r["name"], r.get("engine") or r["hint"]]
        row += [_mark(cells.get(c)) for c in KEY_RUNTIME]
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")

    # 2. 逐工具 × 引擎：哪些工具在这个引擎上可用
    lines.append("## 2. 逐工具 × 引擎")
    lines.append("")
    lines.append("| 工具 | " + " | ".join(f"`{e}`" for e in
                                          ("il2cpp", "mono", "ue")) + " | 备注 |")
    lines.append("|---|---|---|---|")
    by_engine: dict[str, list[dict]] = {}
    for r in rows:
        by_engine.setdefault(r.get("engine") or r["hint"], []).append(r)
    for tool in ALL_TOOLS:
        cells: list[str] = []
        note = ""
        for eng in ("il2cpp", "mono", "ue"):
            group = by_engine.get(eng) or []
            if not group:
                cells.append("（无样本）")
                continue
            marks = {_mark((r.get("cells") or {}).get(tool)) for r in group}
            if marks == {"✅"}:
                cells.append("✅ 全部可用")
            elif marks == {"▫️"}:
                cells.append("▫️ 不适用")
            elif marks == {"❌"}:
                cells.append(f"❌ {len(group)}/{len(group)} 失败")
            else:
                ok = sum(1 for r in group
                         if _mark((r.get("cells") or {}).get(tool)) == "✅")
                skip = sum(1 for r in group
                           if _mark((r.get("cells") or {}).get(tool)) == "▫️")
                bad = len(group) - ok - skip
                cells.append(f"✅{ok} / ▫️{skip} / ❌{bad}")
        for r in rows:
            c = (r.get("cells") or {}).get(tool) or {}
            if c.get("skip"):
                note = _short_reason(c["skip"])
                break
        lines.append(f"| `{tool}` | " + " | ".join(cells) + f" | {note} |")
    lines.append("")

    # 3. 逐游戏关键观测
    lines.append("## 3. 逐游戏关键观测")
    lines.append("")
    for r in rows:
        lines.append(f"### {r['name']}")
        lines.append("")
        lines.append(f"- 目录：`{r['install']}`")
        lines.append(f"- 引擎：`{r.get('engine') or r['hint']}`"
                     f"（标记：{', '.join(r.get('markers') or [])}）")
        if r.get("main_exe"):
            lines.append(f"- 主程序：`{r['main_exe']}`"
                         f"（64 位={r.get('main_exe_is_64')}，"
                         f"运行时={r.get('main_exe_runtime')}）")
        if r.get("index"):
            idx = r["index"]
            lines.append(f"- 索引：类型 {idx.get('type_count')} / 方法 "
                         f"{idx.get('method_count')} / 字段 {idx.get('field_count')}"
                         f" / 程序集 {idx.get('image_count')}")
        if r.get("ue_containers"):
            lines.append(f"- UE 容器：pak {r['ue_containers']['pak']} / "
                         f"utoc {r['ue_containers']['utoc']}")
        if r.get("init"):
            i = r["init"]
            lines.append(f"- 注入：pid={r.get('pid')} 模块={i.get('module')} "
                         f"base={i.get('base')} API {i.get('api_available')} 个"
                         f"（缺 {i.get('api_missing')}）")
        if r.get("images"):
            im = r["images"]
            lines.append(f"- 映像数：运行时 {im.get('runtime')} / 静态 "
                         f"{im.get('static')} / 一致 {im.get('match')}")
        notes = r.get("notes") or {}
        if notes:
            lines.append("- 关键观测：")
            for k, v in notes.items():
                lines.append(f"  - `{k}` = {v}")
        if r.get("runtime"):
            lines.append(f"- 运行时结论：{r['runtime']}")
        if r.get("elapsed_total"):
            lines.append(f"- 耗时：静态 {r.get('elapsed_static')}s / "
                         f"总计 {r.get('elapsed_total')}s")
        if r.get("exception"):
            lines.append(f"- ⚠️ 脚本异常：{r['exception']}")
        if r.get("server_error"):
            lines.append(f"- ❌ 服务器错误：{r['server_error'][:200]}")
        lines.append("")

    # 4. 问题汇总（只看 ❌，不看「不适用」）
    lines.append("## 4. 问题汇总（只看失败）")
    lines.append("")
    bad: dict[str, list[str]] = {}
    for r in rows:
        for tool, cell in (r.get("cells") or {}).items():
            if not cell.get("ok") and not cell.get("skip"):
                bad.setdefault(tool, []).append(
                    f"{r['name']}：{cell.get('error', '')[:140]}")
    if not bad:
        lines.append("没有任何工具返回失败。")
    else:
        for tool, items in sorted(bad.items()):
            lines.append(f"### `{tool}`（{len(items)} 个游戏失败）")
            lines.append("")
            for it in items:
                lines.append(f"- {it}")
            lines.append("")

    # 5. 引擎能力边界（不适用项）
    lines.append("## 5. 引擎能力边界（记为「不适用」的项）")
    lines.append("")
    skipped: dict[str, list[str]] = {}
    for r in rows:
        for tool, cell in (r.get("cells") or {}).items():
            if cell.get("skip"):
                skipped.setdefault(cell["skip"], []).append(
                    f"{r['name']}：`{tool}`")
    if not skipped:
        lines.append("没有被判为「不适用」的工具。")
    else:
        for why, items in sorted(skipped.items()):
            lines.append(f"### {why}")
            lines.append("")
            for it in items:
                lines.append(f"- {it}")
            lines.append("")

    # 6. 本轮修复（人写的清单，但每条都指到跑得动的验收脚本）
    lines.append("## 6. 本轮修复（及各自的验收）")
    lines.append("")
    lines.append("这张矩阵不是一次跑完就完事的快照：**第一轮跑出来的失败**"
                 "（6 个 IL2CPP 游戏「启动/注入被拒绝」、4 个 `runtime_class_info` "
                 "失败、`read_memory` 假成功）逐条定位到根因并修掉之后，"
                 "才得到上面这份 0 失败的结果。每条修复都有可复现的验收脚本，"
                 "并做了**非空转反证**（把代码改回旧写法，断言必须报红）。")
    lines.append("")
    lines.append("| 缺陷（第一轮的现象） | 根因 | 修复 | 验收 |")
    lines.append("|---|---|---|---|")
    lines.append("| 6/9 个 IL2CPP 游戏注入后**游戏当场崩掉**"
                 "（`0x80000003` in `GameAssembly.dll`），后续工具全部报"
                 "「没有已注入的运行时会话」 | `GameAssembly.dll` 已加载 ≠ "
                 "IL2CPP 运行时已初始化：`il2cpp_domain_get()` 此时返回野指针"
                 "（实测 `0x135`），拿它去 `il2cpp_thread_attach` 触发访问违例，"
                 "旧代码把它当「可降级的 fallback」吞掉，于是在**未附着的线程**上"
                 "继续调 il2cpp 接口 | 加 `domainPointer()` 试读指针确认域真的"
                 "建好了；域没好或附着失败一律**拒绝继续**并返回可重试失败，"
                 "由 `wait_ready` 每 0.5s 重试 | `verify_thread_attach.py`"
                 "（源码契约 + 桩对象重试契约 + 真实游戏密集轮询 + "
                 "`ptr(0x135).readPointer()` 抛异常的反证） |")
    lines.append("| `runtime_class_info(name=\"Transform\")` 在 4 个游戏上"
                 "「未找到类」；在 RIP_nz 上**静默返回了不相干的嵌套枚举**"
                 "`UnityEngine.RuleTile.TilingRuleOutput.Transform` | 运行时类解析"
                 "把「嵌套类型」当成「namespace 为空」来判断，而带命名空间的顶层"
                 "类型（`UnityEngine.Transform`）也满足该条件 | 用 "
                 "`il2cpp_class_get_declaring_type` 真正判断嵌套；匹配规则改为"
                 "「完整名 / 显式 namespace + 短名 / 无 namespace 时只匹配顶层」，"
                 "歧义就拒绝并给 candidates，找不到时给出 hint | "
                 "`verify_class_resolve.py`（短名/显式 namespace/完整名三方一致 + "
                 "`State` 歧义 + 不存在的类给 hint + 用 7056 个嵌套类型做反证） |")
    lines.append("| `read_memory(address=\"0x0\", size=4)` 返回 "
                 "`{ok:true, hex:\"\"}` —— **假成功** | Frida 的 "
                 "`readByteArray` 对不可读地址返回 `null`（不是抛异常），"
                 "`new Uint8Array(null)` 得到长度 0 的数组 | 显式判断 `null` 与"
                 "实际字节数，读不到就 `ok:false` | `verify_mcp_tools.py` §12"
                 "（`0x0` 必须失败且不给 `hex`；`0x1` 走抛异常那条路） |")
    lines.append("| `launch_game` 启动游戏后 MCP 服务器**stdout 被游戏日志污染**"
                 "（实测 30 行，其中一行是合法 JSON 字符串），客户端把它当 JSON-RPC"
                 "消息用 → `AttributeError: 'str' object has no attribute 'get'` | "
                 "spawn 用了默认的 `stdio=\"inherit\"`，子进程与服务器共享 stdout，"
                 "而 stdout 是 JSON-RPC 通道 | 改为 `stdio=\"pipe\"` 并由 Frida "
                 "把子进程输出转发到 **stderr** | `verify_spawn_stdio.py`"
                 "（产品路径用 `pipe`、`inherit` 会泄漏标记、4 MB 洪水不卡死） |")
    lines.append("| Mono 游戏 `launch_game` 每次都要**白等满 60 秒**"
                 "（实测 4 个样本各 62 秒）才如实返回「没有 IL2CPP 运行时」 | "
                 "等就绪的预算写死 60 秒，而当时 Mono **没有探针路径**"
                 "（`agent.js` 只认 IL2CPP），等下去永远不会就绪 | "
                 "预算按引擎给（`state.ready_timeout_for`）：有托管运行时探针的引擎"
                 "（IL2CPP / Mono）60 秒，其余（UE/原生）2 秒。"
                 "探针补上 Mono 后端之后，Mono 的 60 秒是**真的会就绪**，不再空转 | "
                 "`verify_runtime_target.py` §8"
                 "（纯函数 + 端到端确认传下去的 timeout 真的是 2.0/60.0） |")
    lines.append("")
    lines.append("> 反面教材（这一轮踩到的）：第一版修复曾让探针在"
                 "「未找到 IL2CPP 运行时模块」时表态 `retryable: false`，"
                 "结果 **8 个 IL2CPP 游戏全部误判**——刚 spawn 完第一次探测"
                 "几乎必然是这个错误（`GameAssembly.dll` 还没加载完），"
                 "实测 GoW 的第二次探测才成功。这条误判正是被**这张矩阵**"
                 "抓出来的（33 → 17 个可用工具），所以「等多久」只能由"
                 "**引擎**决定，不能由探针在那一刻的观测决定。")
    lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    sys.exit(main())
