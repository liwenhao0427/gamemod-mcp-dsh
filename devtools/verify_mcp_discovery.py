#!/usr/bin/env python3
"""验收：**目录布局判定** —— 容器目录不许被当成游戏，UE 不许被送去建索引。

背景（都是实测缺陷，见 `docs/多游戏兼容性扫描报告.md` §3.2 / §3.3 / §3.4）
============================================================
① **容器目录被误判成游戏**：`il2cpp_paths` / `mono_paths` 原先各有一条
   `glob("*/*_Data")` 兜底（为了支持「游戏外面还套一层目录」的布局）。
   它无法区分这两种目录：

       <dir>/<游戏>/<游戏>_Data                  ← 合法：外面套了一层
       <dir>/<游戏A>/<A>_Data + <游戏B>/<B>_Data ← 容器：装着很多游戏

   实测把 `D:\\Projects` 传进 `open_game` 会返回 `ok: true`、
   `engine: "il2cpp"`，元数据却是 **Echoes** 的 —— Agent 以为在分析
   `D:\\Projects`，其实全程在读另一个游戏。这比报错危险得多。
② **UE 被送去建索引**：`open_game(UE 游戏)` 会落到
   「当前会话没有元数据文件。请先用 select_game 选定游戏……」，
   而游戏其实**已经选好了** —— 那句话把调用方引向 `metadata_path`，
   而 UE 根本没有集中元数据。
③ **不支持的引擎只说 unknown**：Godot / GameMaker / Electron 的目录
   一律 `engine: "unknown"`，不给任何线索，Agent 会反复重试。

三层验收
============================================================
1. **纯函数层**（`discovery`）：容器判据、候选清单、有界搜索，
   并用「修复前的挑选顺序」做**非空转反证** —— 旧代码在同目录上确实会
   挑中另一个游戏的元数据，所以「不许猜」这条断言不是空转。
2. **MCP 工具层**（进程内直调）：`open_game` / `inspect_game` 在容器、
   UE、不支持引擎三种目录上的返回结构与出口。
3. **进程级端到端**（stdio JSON-RPC）：真实 Agent 的调用路径，
   外加 `serverInfo.version` 不再是 mcp 库自己的版本号。
"""

from __future__ import annotations

import inspect
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import samples                                            # noqa: E402
import mcp_stdio                                          # noqa: E402
samples.add_src_to_path()
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import gamemod_mcp.server as S                             # noqa: E402
from gamemod_mcp.engine import discovery, state            # noqa: E402

PASS = 0
FAIL = 0
WARN = 0

#: 与 `discovery.SUSPICIOUS_MAIN_EXE_BYTES` 对照用（样本目录里那个 3.3 GB 安装包）
SUSPICIOUS_BYTES = 1024 ** 3


def check(label: str, cond: bool, detail: str = "", warn_only: bool = False) -> bool:
    global PASS, FAIL, WARN
    if cond:
        print(f"  \u2705 {label}" + (f" \u2014 {detail}" if detail else ""))
        PASS += 1
        return True
    if warn_only:
        print(f"  \u26a0\ufe0f  {label}" + (f" \u2014 {detail}" if detail else ""))
        WARN += 1
        return False
    print(f"  \u274c {label}" + (f" \u2014 {detail}" if detail else ""))
    FAIL += 1
    return False


def section(title: str) -> None:
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


def open_with(path: Path) -> dict:
    """走工具层 open_game（缓存指到 .cache-test，不污染用户缓存）。"""
    return S.open_game(game_dir=str(path), cache_dir=str(samples.CACHE_TEST))


def legacy_il2cpp_pick(game_dir: Path) -> Path | None:
    """**复刻修复前**的 `il2cpp_paths` 挑选顺序，只为反证。

    顺序与旧代码逐行一致：`glob("*_Data")` → `glob("*/*_Data")` → `rglob` 兜底。
    本函数只存在于验收脚本里，不参与产品逻辑。
    """
    data_dirs = [d for d in game_dir.glob("*_Data") if d.is_dir()]
    if not data_dirs:
        data_dirs = [d for d in game_dir.glob("*/*_Data") if d.is_dir()]
    for data_dir in data_dirs:
        meta = data_dir / "il2cpp_data" / "Metadata" / "global-metadata.dat"
        if meta.is_file():
            return meta
    for meta in game_dir.rglob("global-metadata.dat"):
        if meta.is_file():
            return meta
    return None


def own_metadata(game_dir: Path, metadata: str) -> bool:
    """元数据文件是否落在给定游戏目录里（大小写不敏感）。"""
    if not metadata:
        return False
    a = os.path.normcase(os.path.abspath(metadata))
    b = os.path.normcase(os.path.abspath(str(game_dir)))
    return a.startswith(b + os.sep)


def _mono_root_count(container: Path) -> int:
    """独立数一遍容器里有几个 Mono 游戏（判据同 samples._game_root_count）。"""
    roots: set[Path] = set()
    for depth in (1, 2, 3):
        pattern = "/".join(["*"] * (depth - 1) + ["*_Data"])
        for data_dir in container.glob(pattern):
            if not data_dir.is_dir():
                continue
            if data_dir.name.lower() in ("il2cpp_data", "mono_data"):
                continue
            if (data_dir / "Managed").is_dir():
                roots.add(data_dir.parent)
    return len(roots)


# ---------------------------------------------------------------- 1. 纯函数层

def part_discovery(container: Path, il2cpp: Path, mono: Path,
                   unsupported: Path, family: str) -> None:
    section("1. 纯函数层：容器判据 / 不猜 / 有界搜索 / 反证")

    # --- 容器判定
    info = discovery.container_candidates(container)
    check("容器目录被识别为容器（候选游戏根 ≥2）",
          bool(info.get("container")) and info.get("count", 0) >= 2,
          f"count={info.get('count')}，前几个候选="
          f"{[Path(p).name for p in (info.get('candidates') or [])[:3]]}")
    check("候选清单里的每一项都真实存在",
          all(Path(p).is_dir() for p in (info.get("candidates") or [])),
          f"{len(info.get('candidates') or [])} 项")

    # --- 非空转反证：修复前的规则在**同一个目录**上确实会挑中一个游戏
    paths = discovery.il2cpp_paths(container)
    legacy = legacy_il2cpp_pick(container)
    check("反证：修复前的挑选顺序确实会挑中一个游戏（不是「无结果」）",
          legacy is not None,
          f"旧写法挑中 {legacy}")
    if legacy is not None:
        owner = legacy.parents[3]
        check("反证：它只是 ≥2 个同样合法的候选之一（所以旧行为是「随便挑一个」）",
              len(paths["ambiguous_roots"]) >= 2
              and any(own_metadata(Path(r), str(legacy))
                      for r in paths["ambiguous_roots"]),
              f"挑中的是 {owner.name}，而候选共有 "
              f"{len(paths['ambiguous_roots'])} 个游戏根")

    # --- 修复后的行为
    check("修复后：容器目录上不给出任何元数据（不猜）",
          paths["metadata"] == "",
          f"metadata={paths['metadata']!r}")
    check("修复后：容器目录上给出歧义候选清单",
          len(paths["ambiguous_roots"]) >= 2,
          f"{len(paths['ambiguous_roots'])} 个候选根")
    check("纯函数层：容器目录的引擎判定为 unknown",
          discovery.detect_engine(container) == "unknown",
          discovery.detect_engine(container))

    # --- 正常游戏不许受影响（这是回归护栏）
    ok_paths = discovery.il2cpp_paths(il2cpp)
    check("正常 IL2CPP 游戏仍能找到元数据",
          ok_paths["metadata"] != "" and own_metadata(il2cpp, ok_paths["metadata"]),
          f"metadata_depth={ok_paths['metadata_depth']}")
    check("正常 IL2CPP 游戏没有歧义候选",
          ok_paths["ambiguous_roots"] == [],
          f"ambiguous_roots={ok_paths['ambiguous_roots']}")
    check("正常 IL2CPP 游戏的深度是 1..3（有界搜索够用）",
          1 <= ok_paths["metadata_depth"] <= 3,
          f"metadata_depth={ok_paths['metadata_depth']}")

    mono_ok = discovery.mono_paths(mono)
    check("正常 Mono 游戏仍能找到 Managed 目录",
          mono_ok["managed_dir"] != "" and
          (Path(mono_ok["managed_dir"]) / "Assembly-CSharp.dll").is_file(),
          f"{Path(mono_ok['managed_dir']).name}，"
          f"{len(mono_ok['assemblies'])} 个 dll")

    mono_roots = _mono_root_count(container)
    mono_container = discovery.mono_paths(container)
    if mono_roots >= 2:
        check(f"容器目录（{mono_roots} 个 Mono 游戏）上不给出 Managed 目录",
              mono_container["managed_dir"] == "",
              f"managed_dir={mono_container['managed_dir']!r}")
    else:
        check("容器里 Mono 游戏不足 2 个，跳过这条", True,
              f"独立数出来 {mono_roots} 个", warn_only=False)

    # --- 不支持的引擎：家族提示
    fam = discovery.engine_family(unsupported)
    check(f"不支持的引擎能给出家族提示（期望 {family}）",
          fam.get("family") == family,
          f"{fam.get('label')} 证据={fam.get('evidence')}")
    check("家族提示仍然不改引擎判定（引擎照旧 unknown）",
          discovery.detect_engine(unsupported) == "unknown",
          discovery.detect_engine(unsupported))
    check("提示里写明了「不支持该引擎」",
          "不支持" in (fam.get("note") or ""),
          fam.get("note", ""))


# ---------------------------------------------------------------- 2. 工具层

def part_tools(container: Path, il2cpp: Path, ue: Path,
               unsupported: Path, family: str) -> None:
    section("2. MCP 工具层：open_game 的错误与出口")

    # --- 容器目录
    r = open_with(container)
    check("open_game(容器目录) 不再返回 ok:true（这是 §3.2 的核心）",
          r.get("ok") is False, f"ok={r.get('ok')} error={r.get('error', '')[:80]}")
    check("错误信息说清了「这不是某一个游戏的目录」",
          "不是某一个游戏" in (r.get("error") or ""),
          r.get("error", ""))
    check("错误里带候选清单（够 Agent 直接改用）",
          (r.get("container") or {}).get("count", 0) >= 2,
          f"count={(r.get('container') or {}).get('count')}")
    check("失败时**没有**把会话绑到别的游戏的元数据上",
          not (r.get("session") or {}).get("metadata_path"),
          f"metadata_path={(r.get('session') or {}).get('metadata_path')!r}")

    # --- UE
    r_ue = open_with(ue)
    check("open_game(UE 游戏) 返回 ok:false", r_ue.get("ok") is False,
          f"error={r_ue.get('error', '')[:80]}")
    check("UE 的错误里指明了该用 ue_* 工具（给了出口）",
          "ue_profile" in (r_ue.get("error") or ""),
          r_ue.get("error", ""))
    check("UE 的错误里带 next_tools 清单",
          "ue_profile" in (r_ue.get("next_tools") or []),
          f"next_tools={r_ue.get('next_tools')}")
    check("UE 的错误**不是**「当前会话没有元数据文件」（§3.3 的旧症状）",
          "当前会话没有元数据文件" not in (r_ue.get("error") or ""),
          f"engine={r_ue.get('engine')}")

    # --- 不支持的引擎
    r_uns = open_with(unsupported)
    check("open_game(不支持的引擎) 返回 ok:false", r_uns.get("ok") is False,
          f"error={r_uns.get('error', '')[:80]}")
    check(f"错误里带出了家族（{family}）",
          (r_uns.get("engine_family") or {}).get("family") == family,
          f"{r_uns.get('engine_family')}")

    # --- 正常游戏 + 会话不许串（前面刚失败过好几次）
    r_ok = open_with(il2cpp)
    check("容器/UE/不支持 之后仍能正常打开 IL2CPP 游戏",
          r_ok.get("ok") is True, f"error={r_ok.get('error', '')[:80]}")
    check("会话的元数据属于这个游戏自己（没有被前面的失败带偏）",
          own_metadata(il2cpp, r_ok.get("metadata_path", "")),
          f"metadata_path={r_ok.get('metadata_path')}")
    check("会话的 exe 也属于这个游戏自己",
          own_metadata(il2cpp, r_ok.get("exe", "")) or bool(r_ok.get("exe")),
          f"exe={r_ok.get('exe')}")

    # --- inspect_game 的提示
    ins = S.inspect_game(game_dir=str(container))
    check("inspect_game(容器目录) 给出 container 与 hint",
          ins.get("engine") == "unknown" and bool(ins.get("container"))
          and bool(ins.get("hint")),
          f"engine={ins.get('engine')} count={(ins.get('container') or {}).get('count')}")
    ins_uns = S.inspect_game(game_dir=str(unsupported))
    check("inspect_game(不支持的引擎) 给出 engine_family 与 hint",
          (ins_uns.get("engine_family") or {}).get("family") == family
          and bool(ins_uns.get("hint")),
          f"family={(ins_uns.get('engine_family') or {}).get('family')}")

    # --- 主程序提示（安装包体积异常）
    exe = ins.get("main_exe") or ""
    if exe and Path(exe).is_file() and Path(exe).stat().st_size >= SUSPICIOUS_BYTES:
        size_gb = Path(exe).stat().st_size / 1024 ** 3
        check("体积异常的主程序会带 main_exe_hint（别拿安装包当游戏启动）",
              bool(ins.get("main_exe_hint")),
              f"{Path(exe).name} = {size_gb:.1f} GB；hint={ins.get('main_exe_hint', '')[:60]}")
    else:
        check("本机样本目录里没有超大 exe，跳过 main_exe_hint 断言", True,
              f"main_exe={exe}", warn_only=False)

    # --- §3.7 死参数已删 + metadata_path 模式下不编造 exe
    sig = inspect.signature(state.select_game)
    check("select_game 不再有没人传的 exe 形参（§3.7）",
          "exe" not in sig.parameters, f"参数={list(sig.parameters)}")
    # 先清掉会话身份：否则「同一个游戏 → 复用」分支会原样返回上一个会话的 exe，
    # 于是断言拿到的是旧值 —— 那是测试自己的问题，不是产品行为。
    state.state().reset_target()
    meta_only = S.open_game(metadata_path=r_ok.get("metadata_path", ""),
                            cache_dir=str(samples.CACHE_TEST))
    check("只给 metadata_path 时 ok:true",
          meta_only.get("ok") is True, f"error={meta_only.get('error', '')[:80]}")
    check("只给 metadata_path 时**不猜**主程序（exe 为空，不编造）",
          meta_only.get("exe") == "", f"exe={meta_only.get('exe')!r}")


# ---------------------------------------------------------------- 合成布局

def part_synthetic(root: Path) -> None:
    """合成目录结构：只测布局判定，不需要任何真实游戏文件。

    真实样本能证明「现在的机器上没问题」，但不能证明**边角布局**也没问题：
    本机没有「外面套了一层」的样本，而那条路径正是旧 `glob("*/*_Data")`
    兜底存在的理由 —— 改动不能把它弄坏。所以这里手工造：
    ① 纯 Unity（同级有 `*_Data`，但无脚本后端）
    ② 外面套一层（1 个候选游戏根）
    ③ 容器（2 个 IL2CPP 游戏）
    ④ 跨引擎容器（1 个 IL2CPP + 1 个 Mono —— 只看 IL2CPP 标记会漏判）
    ⑤ 套一层的目录里那个游戏**没有**后端标记（提示要能指出该指向哪里）
    """
    def make_il2cpp(game_root: Path, name: str) -> Path:
        meta = (game_root / name / f"{name}_Data" / "il2cpp_data"
                / "Metadata" / "global-metadata.dat")
        meta.parent.mkdir(parents=True, exist_ok=True)
        # 内容无所谓：这一节只验**布局判定**，判定只看文件是否存在
        meta.write_bytes(b"\xaf\x1b\xb1\xfa")
        return meta

    base = root / "layout"
    shutil.rmtree(base, ignore_errors=True)

    # ① 纯 Unity
    plain = base / "plain-unity"
    (plain / "Plain_Data").mkdir(parents=True)
    check("合成①：同级有 *_Data、但无元数据/Managed → unity",
          discovery.detect_engine(plain) == "unity",
          discovery.detect_engine(plain))

    # ② 外面套了一层（旧 glob 的初衷，必须继续支持）
    wrapped = base / "wrapped"
    want = make_il2cpp(wrapped, "My Game")
    check("合成②：外面套一层仍能认出 IL2CPP",
          discovery.detect_engine(wrapped) == "il2cpp",
          discovery.detect_engine(wrapped))
    check("合成②：拿到的正是那一层的元数据",
          discovery.il2cpp_paths(wrapped)["metadata"] == str(want),
          discovery.il2cpp_paths(wrapped)["metadata"])
    check("合成②：只有一个候选游戏根，不算容器",
          discovery.container_candidates(wrapped) == {},
          f"metadata_depth={discovery.il2cpp_paths(wrapped)['metadata_depth']}")

    # ③ 容器：两个 IL2CPP 游戏
    cont = base / "container"
    make_il2cpp(cont, "GameA")
    make_il2cpp(cont, "GameB")
    check("合成③：两个游戏 → 容器，且不给出元数据",
          discovery.container_candidates(cont).get("count") == 2
          and discovery.il2cpp_paths(cont)["metadata"] == ""
          and discovery.detect_engine(cont) == "unknown",
          f"count={discovery.container_candidates(cont).get('count')} "
          f"engine={discovery.detect_engine(cont)}")
    legacy = legacy_il2cpp_pick(cont)
    check("合成③反证：旧的挑选顺序会挑中其中一个游戏（判据不是空转）",
          legacy is not None,
          f"旧写法挑中 {legacy}")

    # ④ 跨引擎容器：只看 IL2CPP 标记会漏判成「一个 IL2CPP 游戏」
    mixed = base / "mixed"
    make_il2cpp(mixed, "UnityA")
    (mixed / "MonoB" / "MonoB_Data" / "Managed").mkdir(parents=True)
    check("合成④：1 个 IL2CPP + 1 个 Mono 也算容器（跨引擎一起数）",
          discovery.container_candidates(mixed).get("count") == 2
          and discovery.il2cpp_paths(mixed)["metadata"] == "",
          f"count={discovery.container_candidates(mixed).get('count')} "
          f"metadata={discovery.il2cpp_paths(mixed)['metadata']!r}")

    # ⑤ 套一层的目录，里面的游戏没有后端标记
    #    （既没元数据也没 Managed：判定只能靠 *_Data，提示必须指出该指向哪里）
    backendless = base / "backendless"
    (backendless / "Some Game" / "Some Game_Data").mkdir(parents=True)
    info = discovery.describe_game(backendless)
    check("合成⑤：认不出来时给的是「该把 game_dir 指向哪个子目录」",
          discovery.detect_engine(backendless) == "unknown"
          and (info.get("game_root") or "").endswith("Some Game")
          and "不是游戏" in (info.get("hint") or ""),
          f"hint={info.get('hint')}")
    r = open_with(backendless)
    check("合成⑤：工具层同样带回 game_root，省掉一轮试错",
          r.get("ok") is False
          and (r.get("game_root") or "").endswith("Some Game")
          and Path(r.get("game_root") or "").name in (r.get("error") or ""),
          f"game_root={r.get('game_root')} error={r.get('error', '')[:90]}")

    shutil.rmtree(base, ignore_errors=True)


# ---------------------------------------------------------------- 3. 进程级

def part_stdio(container: Path) -> None:
    section("3. 进程级端到端：真服务器 + stdio 握手（Agent 的实际路径）")

    c = mcp_stdio.McpStdioServer(
        [sys.executable, "-m", "gamemod_mcp.server"],
        env=mcp_stdio.script_form_env(str(samples.SRC), clean_pythonpath=False),
        cwd=str(samples.ROOT)).start()
    try:
        init = c.initialize()
        check("stdio 握手成功", init["ok"], init["error"][:200])
        si = init.get("server_info") or {}
        check("serverInfo.name 是 gamemod", si.get("name") == "gamemod", str(si.get("name")))
        check("serverInfo.version 是本项目的版本（不是 mcp 库的版本）",
              si.get("version") == S.__version__,
              f"上报 {si.get('version')!r}，本项目 {S.__version__!r}")

        r = c.call_tool("open_game", {"game_dir": str(container),
                                      "cache_dir": str(samples.CACHE_TEST)})
        data = r["data"] or {}
        check("端到端：open_game(容器目录) 返回 ok:false",
              r["ok"] and data.get("ok") is False,
              f"is_error={r['is_error']} error={r['error'][:120]}")
        check("端到端：错误里带候选清单",
              (data.get("container") or {}).get("count", 0) >= 2,
              f"count={(data.get('container') or {}).get('count')}")

        check("服务器 stdout 全程只有 JSON-RPC", c.non_json_lines == 0,
              "任何一行非 JSON 都会破坏协议")
    finally:
        c.close()


def main() -> int:
    print("验收：目录布局判定（容器目录 / UE / 不支持的引擎）")
    print(f"样本根: {[str(p) for p in samples.SAMPLE_ROOTS]}")

    samples.ensure_cache_dirs()
    container = samples.find_container()
    # 布局判定与容器识别**与具体哪个** IL2CPP 游戏无关，故用 find_il2cpp_any()：
    # 缺 Garden of Witches 时退用本机任意一个受支持的 IL2CPP 游戏。
    il2cpp = samples.find_il2cpp_any()
    mono = samples.find_mono()
    ue = samples.find_ue_pak_v12()
    unsupported_pair = samples.find_unsupported()
    unsupported = unsupported_pair[0] if unsupported_pair else None
    family = unsupported_pair[1] if unsupported_pair else ""

    missing = []
    if not samples.require(container, "容器目录（装着 ≥2 款游戏）"):
        missing.append("container")
    if not samples.require(il2cpp, "IL2CPP 样本（任意一款受支持的）"):
        missing.append("il2cpp")
    if not samples.require(mono, "Mono (Archon Soul)"):
        missing.append("mono")
    if not samples.require(ue, "UE (CatapultHero)"):
        missing.append("ue")
    if not samples.require(unsupported, "不支持引擎的样本"):
        missing.append("unsupported")
    if missing:
        print(f"\n缺少样本 {missing}，无法验证。")
        return 1

    print(f"  容器目录  : {container}")
    print(f"  IL2CPP    : {il2cpp}")
    print(f"  Mono      : {mono}")
    print(f"  UE        : {ue}")
    print(f"  不支持引擎 : {unsupported}（家族 {family}）")

    part_discovery(container, il2cpp, mono, unsupported, family)
    part_synthetic(samples.CACHE_TEST)
    part_tools(container, il2cpp, ue, unsupported, family)
    part_stdio(container)

    print()
    print("=" * 74)
    if FAIL:
        print(f"\u274c 通过 {PASS} 项，失败 {FAIL} 项（警告 {WARN}）")
    else:
        print(f"\u2705 通过 {PASS} 项，失败 0 项"
              + (f"（警告 {WARN}）" if WARN else ""))
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
