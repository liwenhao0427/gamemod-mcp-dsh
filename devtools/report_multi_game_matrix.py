#!/usr/bin/env python3
"""多游戏兼容性矩阵：扫描样本根下的**每一个游戏**，逐阶段记录 MCP 生效到哪一步。

为什么需要它
============================================================
`verify_mcp_<引擎>.py` 证明的是「**样本那几个**游戏能跑通」——
IL2CPP 用 Garden of Witches、Mono 用 Archon Soul、UE 用 CatapultHero / Dinoblade。
它回答不了「换一个游戏还行不行」：样本是被挑过的，而真实使用场景是
**本机上随手一个游戏目录**。

这个脚本把本机样本根（默认 `devtools/samples.py` 的 SAMPLE_ROOTS）下
所有带引擎标记的目录都当成被测对象，对每个游戏按**固定阶段**依次调用
MCP 工具，记录每一步的 ok / 失败原因 / 耗时，最后给出矩阵。

它是**报告**不是验收：没有「通过/失败」，只有实测数字，所以叫 `report_*`
而不是 `verify_*`，也不进 `run_all_verify.py`（19 个游戏逐个建索引太慢，
不适合当每次提交的门禁）。

阶段定义（为什么按这个顺序）
============================================================
    ① inspect_game        纯静态：识别引擎、找主程序
    ② open_game           建索引：静态元数据 → SQLite
    ③ list_images         程序集清单
    ④ search_classes      类查询（total 必须 > 0）
    ⑤ class_detail        字段/方法/token
    ⑥ search_fields       按字段名找（Mod 最常用的入口）
    ⑦ find_derived_classes 继承过滤（Unity 才有意义）
    ⑧ inspect_binary      PE 解析
    ⑨ decompile           反编译（Mono 才有意义，依赖 ilspycmd）
    ⑩ ue_*                UE 容器清单（UE 才有意义）

前 ① ~ ④ 是「能不能用」的分水岭：

- ① 失败 = 引擎识别不了（Godot / 自研引擎 / 打包结构不标准）
- ② 失败 = 认识引擎但元数据解析不了（**最值得修的一类**）
- ④ total=0 = 索引建起来了但查不到东西（静默的坏结果，比报错更危险）

用法
============================================================
    python devtools\\report_multi_game_matrix.py
    python devtools\\report_multi_game_matrix.py --roots D:\\Projects
    python devtools\\report_multi_game_matrix.py --only "Archon Soul" --verbose
    python devtools\\report_multi_game_matrix.py --json .cache-test\\matrix.json

退出码**恒为 0**（它是报告，不是门禁）。要看红/绿请用 verify_*.py。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import timedelta
from pathlib import Path

# 中文控制台默认 GBK，打印 ✅ / 表格字符会抛 UnicodeEncodeError
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001 - 老版本 Python 没有 reconfigure
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
import samples  # noqa: E402  （同目录，路径已就位）

samples.add_src_to_path()

ROOT = samples.ROOT
#: 以**模块**方式启动。注意不能写成 `python src/gamemod_mcp/server.py`：
#: 那样 `__package__` 是 None，`from . import __version__` 会直接 ImportError
#: （实测，见报告里的「入口」一节）。正确形式是 `-m gamemod_mcp.server` + PYTHONPATH。
SERVER_MODULE = "gamemod_mcp.server"
#: 矩阵专用缓存目录（已被 .gitignore 的 `.cache-*` 覆盖）
CACHE = ROOT / ".cache-matrix"

#: 走查的最大目录深度（相对样本根）。
#: 为什么是 6：实测本机最深的样本是
#: `<根>/Everything.is.Crab.v1.0.1/Everything.is.Crab.v1.0.1/<X>_Data/il2cpp_data/Metadata`
#: —— metadata 在**第 5 层**。曾经的 4 会让这一类「多套一层目录的解压包」整批漏掉，
#: 而漏掉在报告里表现为「本机没有 IL2CPP 游戏」，比报错更难发现。
MAX_DEPTH = 6

#: 单个工具调用的超时。大游戏的索引构建实测可达几十秒，给足余量。
CALL_TIMEOUT = timedelta(seconds=900)


# ---------------------------------------------------------------- 候选发现


def _walk(root: Path, max_depth: int = MAX_DEPTH):
    """按深度受限地遍历目录（只下钻目录，不 stat 无关文件）。"""
    root = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        try:
            depth = len(here.relative_to(root).parts)
        except ValueError:
            continue
        if depth >= max_depth:
            dirnames[:] = []
        yield here, dirnames, filenames


def _install_root_from_data_dir(data_dir: Path) -> Path | None:
    """从 `X_Data` 目录反推安装根（`X_Data` 的上一级）。"""
    p = data_dir
    while p.parent != p:
        if p.name.endswith("_Data"):
            return p.parent
        p = p.parent
    return None


def discover_games(roots: list[Path]) -> list[dict]:
    """在样本根下找出所有「像游戏」的安装根。

    判据只看**引擎标记文件**，不做任何猜测：
      · `global-metadata.dat`             → IL2CPP
      · `<X>_Data/Managed/*.dll`          → Mono
      · `<Proj>/Content/Paks/*.pak|.utoc` → UE
    """
    found: dict[str, dict] = {}

    def add(install: Path | None, marker: str, engine_hint: str) -> None:
        if install is None or not install.is_dir():
            return
        key = str(install).lower()
        rec = found.setdefault(key, {
            "install": str(install),
            "markers": [],
            "hint": engine_hint,
        })
        rec["markers"].append(marker)
        # IL2CPP / Mono 的判定优先于 UE：一个目录同时有 pak 与 metadata 时，
        # 真正能索引的是 metadata（UE 的 pak 只是资源）。
        if engine_hint in ("il2cpp", "mono"):
            rec["hint"] = engine_hint

    for root in roots:
        if not root.is_dir():
            continue
        for here, dirnames, filenames in _walk(root):
            names = set(filenames)
            if "global-metadata.dat" in names:
                # here = <install>/<X>_Data/il2cpp_data/Metadata
                # here.parents[1] = <X>_Data → 由它反推安装根
                add(_install_root_from_data_dir(here.parents[1]),
                    "global-metadata.dat", "il2cpp")
            if "Managed" in dirnames and here.name.endswith("_Data"):
                add(_install_root_from_data_dir(here), "Managed/*.dll", "mono")
            if here.name == "Paks":
                has_ue = any(n.endswith(".pak") or n.endswith(".utoc") for n in filenames)
                if has_ue:
                    # <install>/<Proj>/Content/Paks
                    add(here.parents[2] if len(here.parents) >= 3 else None,
                        "Content/Paks/*", "ue")

    return sorted(found.values(), key=lambda r: r["install"].lower())


def _dedupe_nested(cands: list[dict]) -> list[dict]:
    r"""丢掉「被另一个同引擎候选包住」的候选。

    实测噪声：`D:\Projects\Dinoblade` 与 `D:\Projects\Dinoblade\Engine\Programs`
    都带 `Content/Paks/*`（UE 引擎自带的程序目录里也有 pak）——
    后者不是游戏，多一行会让人以为发现了两个 UE 游戏。
    """
    def is_ancestor(a: str, b: str) -> bool:
        try:
            return Path(a).resolve() in Path(b).resolve().parents
        except OSError:
            return False

    out: list[dict] = []
    for c in cands:
        shadowed = any(o is not c and o["hint"] == c["hint"]
                       and is_ancestor(o["install"], c["install"]) for o in cands)
        if not shadowed:
            out.append(c)
    return out


#: 明显是**工具/构建产物**目录名，不是游戏。留着它们会让矩阵里
#: 「不支持的引擎」一大半是 steamcmd / 反编译工具，掩盖真正想问的
#: 「本机有几个游戏本 MCP 吃不下」。被排除的目录会原样打印出来，不静默丢弃。
TOOL_DIR_NAMES = {".tools", "dist", "build", "分享文件夹", "obj", "bin", "node_modules"}


def _is_tool_dir(p: Path) -> bool:
    return any(part.lower() in TOOL_DIR_NAMES for part in p.parts)


#: 判定「疑似有游戏、但没有 Unity/UE 引擎标记」的体积门槛。
#: 20 MB 的依据：本机所有 Unity/UE 游戏主程序都 ≥ 60 MB，而工具类 exe
#: （steamcmd / 代理 / 反编译器）多在 10 MB 以下。它是**筛子**不是判据 ——
#: 这些目录里引擎不认识的那些，结论如实写成「不认识这个引擎」。
BIG_EXE_MB = 20


def discover_non_engine(roots: list[Path], known: list[dict]) -> tuple[list[dict], list[str]]:
    """找出「有大 exe、但没有引擎标记」的目录。

    存在的意义：只报「支持的游戏」会让人误以为 MCP 覆盖了本机全部游戏。
    这一类目录（Godot / GameMaker / Electron / 工具）也要有行，
    结论写「不支持的引擎」。

    Returns:
        (候选列表, 被当作工具目录跳过的路径列表)
    """
    known_roots = [Path(k["install"]).resolve() for k in known]

    def covered(p: Path) -> bool:
        for k in known_roots:
            if p == k or k in p.parents:
                return True
        return False

    seen: dict[str, dict] = {}
    skipped: list[str] = []
    for root in roots:
        if not root.is_dir():
            continue
        for here, _dirnames, filenames in _walk(root, max_depth=4):
            if covered(here):
                continue
            for n in filenames:
                if not n.lower().endswith(".exe"):
                    continue
                if n.lower().startswith(("unins", "unitycrashhandler", "crashpad", "vcredist", "dxsetup")):
                    continue
                try:
                    if (here / n).stat().st_size < BIG_EXE_MB * 1024 * 1024:
                        continue
                except OSError:
                    continue
                if _is_tool_dir(here):
                    skipped.append(str(here))
                    break
                seen.setdefault(str(here).lower(), {
                    "install": str(here),
                    "markers": [f"{n} ≥ {BIG_EXE_MB}MB"],
                    "hint": "unknown",
                })
                break
    return (sorted(seen.values(), key=lambda r: r["install"].lower()), skipped)


def _family_counts(rows: list[dict]) -> dict[str, int]:
    """按「报告侧猜的引擎家族」统计（只用于不支持的引擎，给改进方向）。"""
    fam: dict[str, int] = {}
    for r in rows:
        key = (r.get("lookalike") or "未判定").split("（")[0]
        fam[key] = fam.get(key, 0) + 1
    return fam


def guess_lookalike(install: Path) -> str:
    """本 MCP 不支持这些引擎，但**报告**应该说清它到底是什么。

    这是报告侧的判断（有标记文件为证），不是产品能力 —— 产品只声明支持
    IL2CPP / Mono / UE，返回 `engine: "unknown"` 是诚实的；但对使用者来说
    「Godot」和「完全没认出来」是两件事，值得分开写。
    """
    def has(*pats: str) -> bool:
        for pat in pats:
            try:
                if next(iter(install.glob(pat)), None) is not None:
                    return True
            except OSError:
                pass
        return False

    if has("*.pck", "data_*_windows_*", "data_*_windows_x86_64"):
        return "Godot（有 .pck / data_<name>_windows_* 导出目录）"
    if has("data.win"):
        return "GameMaker Studio（有 data.win）"
    if has("chrome_100_percent.pak", "resources/app.asar", "*.dll", "locales/*.pak"):
        if has("chrome_100_percent.pak", "resources/app.asar"):
            return "Electron / Chromium（有 chrome_*.pak / app.asar）"
    if has("*.pdb", "*.dll"):
        return "自研 / 原生（无 Unity、Godot、GameMaker 标记）"
    return "未能判定"


# ---------------------------------------------------------------- MCP 客户端


def unwrap(result) -> dict:
    """把 MCP 的 CallToolResult 还原成工具返回的那个 dict。"""
    if getattr(result, "structuredContent", None):
        sc = result.structuredContent
        # FastMCP 对 dict 返回可能包一层 {"result": {...}}
        if isinstance(sc, dict) and set(sc.keys()) == {"result"} and isinstance(sc["result"], dict):
            return sc["result"]
        if isinstance(sc, dict):
            return sc
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if not text:
            continue
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"ok": False, "error": f"非 JSON 返回: {text[:200]}"}
    return {"ok": False, "error": "空返回"}


def brief(obj, limit: int = 300) -> str:
    """把任意返回值压成一行摘要，供报告使用。"""
    s = json.dumps(obj, ensure_ascii=False)
    return s if len(s) <= limit else s[:limit] + "…"


class Stages:
    """一个游戏的阶段记录器。"""

    ORDER = [
        ("inspect_game", "探测"),
        ("open_game", "索引"),
        ("list_images", "程序集"),
        ("search_classes", "类查询"),
        ("class_detail", "类详情"),
        ("search_fields", "字段"),
        ("derived", "继承"),
        ("inspect_binary", "PE"),
        ("decompile", "反编译"),
        ("ue_detect", "UE识别"),
        ("ue_profile", "UE概况"),
        ("ue_entries", "UE清单"),
    ]

    def __init__(self, install: str, hint: str):
        self.install = install
        self.hint = hint
        self.stages: dict[str, dict] = {}
        self.notes: list[str] = []
        self.engine = ""
        self.lookalike = ""
        self.counts: dict[str, int] = {}

    def record(self, stage: str, status: str, ms: int, detail: str = "") -> None:
        self.stages[stage] = {"status": status, "ms": ms, "detail": detail}

    def get(self, stage: str) -> dict:
        return self.stages.get(stage, {"status": "未执行", "ms": 0, "detail": ""})


async def call(session, name: str, args: dict, st: Stages, stage: str, verbose: bool) -> dict:
    """调一个工具并把结果记进阶段表。返回工具返回的 dict。"""
    t0 = time.perf_counter()
    try:
        res = unwrap(await session.call_tool(name, args, read_timeout_seconds=CALL_TIMEOUT))
    except Exception as exc:  # noqa: BLE001 - 任何异常都算这一阶段失败
        ms = int((time.perf_counter() - t0) * 1000)
        st.record(stage, "异常", ms, f"{type(exc).__name__}: {exc}")
        if verbose:
            print(f"      ✗ {name} 异常: {exc}")
        return {"ok": False, "error": str(exc)}

    ms = int((time.perf_counter() - t0) * 1000)
    ok = bool(res.get("ok"))
    st.record(stage, "ok" if ok else "fail", ms, brief(res.get("error") if not ok else res))
    if verbose:
        print(f"      {'✓' if ok else '✗'} {name}  {ms} ms  {brief(res, 160) if not ok else ''}")
    return res


async def probe_game(session, cand: dict, verbose: bool) -> Stages:
    """对一个候选安装根跑阶段链。

    引擎不认识时会在探测之后自然停下（后面的阶段记「不适用」）——
    不再有单独的 detect_only 模式：实测那样会把 `inspect_game` 判成 il2cpp
    的**误报**（`D:\\Projects` 这种父目录）也一并跳过，把真问题藏起来。
    """
    install = Path(cand["install"])
    st = Stages(cand["install"], cand["hint"])
    st.notes.append("标记: " + ", ".join(cand["markers"]))

    def skip_from(first: str, reason: str) -> None:
        """把 first 及其之后的阶段一律记为「不适用」。"""
        hit = False
        for key, _lab in Stages.ORDER:
            if key == first:
                hit = True
            if hit and key not in st.stages:
                st.record(key, "不适用", 0, reason)

    # ① inspect_game —— 纯静态
    info = await call(session, "inspect_game", {"game_dir": str(install)}, st, "inspect_game", verbose)
    engine = info.get("engine", "") if info.get("ok") else ""
    st.engine = engine or "?"
    if info.get("ok"):
        st.counts["main_exe_found"] = 1 if info.get("main_exe") else 0
        if not info.get("main_exe"):
            st.notes.append("未找到主程序 exe")
    else:
        st.notes.append(f"探测失败: {info.get('error', '')[:120]}")
        skip_from("open_game", "探测失败")
        return st

    # ⑧ inspect_binary 提到前面做：即使引擎不认识，PE 结论也是有用证据
    exe = info.get("main_exe", "")
    if exe:
        binr = await call(session, "inspect_binary", {"path": exe}, st, "inspect_binary", verbose)
        if binr.get("ok"):
            st.notes.append(f"主程序运行时: {binr.get('runtime')}，"
                            f"is_dotnet={binr.get('is_dotnet')}，is_64={binr.get('is_64')}")
    else:
        st.record("inspect_binary", "跳过", 0, "没有主程序")

    if st.engine not in ("il2cpp", "mono", "unity", "ue"):
        st.lookalike = guess_lookalike(install)
        st.notes.append(f"引擎识别为 `{st.engine}` —— 本 MCP 不支持；"
                        f"报告侧判断它是：{st.lookalike}")
        skip_from("open_game", f"不支持的引擎: {st.engine}")
        return st

    # ② open_game —— 建索引（缓存目录独立，避免误命中别人的缓存）
    opened = await call(session, "open_game",
                        {"game_dir": str(install), "cache_dir": str(CACHE)},
                        st, "open_game", verbose)
    if st.engine == "ue":
        # UE **没有集中元数据**，所以「建索引」这一步本来就不适用
        # （SKILL.md §UE 明确写了跳过 open_game，直接走 ue_* 工具）。
        # 但实测它返回的是 ok:false —— 把真实报错原文留在 detail 里，
        # 不打折扣也不掩盖。
        #
        # 报错原文会随会话状态变：清掉「上一个游戏的元数据路径」这个泄漏之前，
        # 这里报的是「元数据解析失败：不支持的引擎类型：'ue'」
        # （拿着陈旧的 IL2CPP 元数据 + engine='ue' 去开索引的结果 ——
        #  那条报错本身就是泄漏的症状）；修掉泄漏之后才露出真正的
        # 「当前会话没有元数据文件。请先用 select_game 选定游戏…」。
        # 两种都不是好文案，所以这里只如实转述当次拿到的那个字符串。
        raw = st.get("open_game")
        st.record("open_game", "不适用", raw["ms"],
                  "UE 无集中元数据（设计如此）。但工具返回的是失败："
                  + (opened.get("error") or "")[:160])
        st.notes.append("UE：open_game 返回 ok=false，实际会话仍被选中（ue_profile 后来能取到主程序）")
    elif not opened.get("ok"):
        st.notes.append(f"建索引失败: {opened.get('error', '')[:200]}")
        skip_from("list_images", "建索引失败")
        return st

    # ---- UE 路线：类型系统不存在，只有容器工具
    if st.engine == "ue":
        skip_from("list_images", "UE 无类型元数据，不走类查询")
        det = await call(session, "detect_ue", {}, st, "ue_detect", verbose)
        prof = await call(session, "ue_profile", {}, st, "ue_profile", verbose)
        st.notes.append(f"detect_ue ok={det.get('ok')} confidence={det.get('confidence')}")
        first_container = ""
        if prof.get("ok"):
            # ue_profile 的容器在两个键下：pak（.pak 报告）与 iostore（.utoc 报告）
            conts = (prof.get("pak") or []) + (prof.get("iostore") or [])
            st.counts["ue_containers"] = len(conts)
            st.counts["ue_is_ue"] = 1 if prof.get("is_ue") else 0
            for c in conts:
                first_container = c.get("pak") or c.get("utoc") or c.get("path") or ""
                if first_container:
                    break
        else:
            st.notes.append(f"ue_profile 失败: {prof.get('error') or prof.get('reason')}")
        if first_container:
            ent = await call(session, "ue_list_entries",
                             {"container": first_container, "limit": 5},
                             st, "ue_entries", verbose)
            if ent.get("ok"):
                st.counts["ue_entries_sample"] = ent.get("total", 0)
        else:
            st.record("ue_entries", "跳过", 0, "profile 里没拿到容器路径")
        # 另外两个 UE 工具不计入矩阵列，但失败要留痕（不记 = 假装它们没问题）
        scr = await call(session, "ue_script_objects", {}, st, "ue_script", verbose)
        man = await call(session, "ue_manifest", {}, st, "ue_manifest_extra", verbose)
        st.notes.append(f"ue_script_objects ok={scr.get('ok')}、ue_manifest ok={man.get('ok')}")
        return st

    # ---- Unity 路线
    idx = opened.get("index", {}) or {}
    st.engine = opened.get("engine") or st.engine
    for k in ("type_count", "method_count", "field_count"):
        if k in idx:
            st.counts[k] = idx[k]
    st.counts["index_elapsed_ms"] = int((opened.get("elapsed") or 0) * 1000)
    st.notes.append(f"索引 reused={opened.get('reused')}")
    if not st.counts.get("type_count"):
        st.notes.append("⚠️ 索引里一个类型都没有")

    # ③ list_images
    imgs = await call(session, "list_images", {"limit": 200}, st, "list_images", verbose)
    if imgs.get("ok"):
        st.counts["images"] = imgs.get("total", len(imgs.get("items", []) or []))

    # ④ search_classes（空 pattern = 全部）
    classes = await call(session, "search_classes", {"pattern": "", "limit": 50},
                         st, "search_classes", verbose)
    if classes.get("ok"):
        st.counts["classes_total"] = classes.get("total", 0)
        if not classes.get("total"):
            st.notes.append("⚠️ search_classes 返回 total=0")

    # ⑤ class_detail —— 选一个**真实游戏类**，而不是索引里 index 最小的那个。
    #    踩过的两个坑（都不是产品的锅，是选择器的锅）：
    #      · IL2CPP：挑到 `$BurstDirectCallInitializer`，它在 6 个程序集里同名
    #        → class_detail 正确地拒绝了（`ok:false` + 候选清单）
    #      · Mono：挑到 `__StaticArrayInitTypeSize=12`（Roslyn 产物，0 方法），
    #        拿它去 decompile 必然「找不到类型」
    #    所以改成：**MonoBehaviour 派生类优先**（那才是 Mod 要找的东西），
    #    并且逐个重试，直到有一个真的查得出来。
    der0 = await call(session, "find_derived_classes",
                      {"base_full_name": "UnityEngine.MonoBehaviour", "limit": 10},
                      st, "derived", verbose)
    if der0.get("ok"):
        st.counts["monobehaviour_total"] = der0.get("total", 0)

    def _generated(item: dict) -> bool:
        nn = item.get("nested_name") or item.get("full_name") or ""
        return any(ch in nn for ch in "<>") or nn.startswith(("__", "$", "="))

    items = (classes.get("items") or []) if classes.get("ok") else []
    pool: list[str] = []
    for it in (der0.get("items") or []) if der0.get("ok") else []:
        if not _generated(it):
            pool.append(it["full_name"])
    for it in sorted([i for i in items if not _generated(i) and (i.get("method_count") or 0) > 0],
                     key=lambda i: i["method_count"]):
        pool.append(it["full_name"])
    for it in items:
        if not _generated(it):
            pool.append(it["full_name"])
    pool = list(dict.fromkeys(pool))[:6]

    sample_class = ""
    detail = {"ok": False, "error": "没有可用的类"}
    tried: list[str] = []
    for name_to_try in pool:
        tried.append(name_to_try)
        detail = await call(session, "class_detail",
                            {"full_name": name_to_try, "include_inherited": False},
                            st, "class_detail", verbose)
        if detail.get("ok"):
            sample_class = name_to_try
            n_f = len(detail.get("fields", []) or [])
            n_m = len(detail.get("methods", []) or [])
            st.counts["detail_fields"] = n_f
            st.counts["detail_methods"] = n_m
            st.notes.append(f"样例类: {name_to_try}（{n_f} 字段 / {n_m} 方法）")
            st.notes.append(f"class_detail 试了 {len(tried)} 个名字才成功"
                            if len(tried) > 1 else "class_detail 一次命中")
            break
    if not detail.get("ok"):
        st.notes.append(f"class_detail 连试 {len(tried)} 个类都失败；"
                        f"最后一个的错误：{brief(detail.get('error'), 160)}")

    # ⑥ search_fields
    fld = await call(session, "search_fields", {"pattern": "Health", "limit": 5},
                     st, "search_fields", verbose)
    if fld.get("ok"):
        st.counts["fields_matching_Health"] = fld.get("total", 0)

    # ⑦ find_derived_classes —— 已在 ⑤ 之前跑过（用它挑样例类），这里不重复调用

    # ⑨ decompile —— 只有 Mono 有意义（需要 ilspycmd）
    if st.engine == "mono":
        mono = (info.get("mono") or {}) if info.get("ok") else {}
        asm = mono.get("game_assembly") or ""
        if asm and sample_class:
            dec = await call(session, "decompile",
                             {"assembly": asm, "type_name": sample_class},
                             st, "decompile", verbose)
            if dec.get("ok"):
                st.notes.append(f"反编译《{Path(asm).name}》.{sample_class} "
                                f"→ {len(dec.get('source', '') or '')} 字符")
        elif asm:
            dec = await call(session, "decompile",
                             {"assembly": asm, "output_dir": str(CACHE / "decompiled")},
                             st, "decompile", verbose)
        else:
            st.record("decompile", "跳过", 0, "没挑出游戏程序集")
    else:
        st.record("decompile", "不适用", 0, "非 Mono 引擎")

    # ⑩ UE 阶段对 Unity 不适用
    for key in ("ue_detect", "ue_profile", "ue_entries"):
        st.record(key, "不适用", 0, "非 UE 引擎")

    return st


# ---------------------------------------------------------------- 报告


def reach_of(st: Stages) -> str:
    """给出「生效到哪一步」的一句话结论。"""
    if st.get("inspect_game")["status"] != "ok":
        return "✗ 探测即失败（目录不存在 / 无法读取）"
    if st.engine == "unity":
        return "△ Unity 目录，但没找到 IL2CPP/Mono 元数据"
    if st.engine not in ("il2cpp", "mono", "ue"):
        fam = (st.lookalike or "未判定").split("（")[0]
        return f"✗ 不支持的引擎（{st.engine}；实际上像 {fam}）"

    if st.engine == "ue":
        subs = [("ue_detect", "识别"), ("ue_profile", "概况"), ("ue_entries", "清单")]
        done = [lab for k, lab in subs if st.get(k)["status"] == "ok"]
        bad = [lab for k, lab in subs if st.get(k)["status"] in ("fail", "异常")]
        if not bad and len(done) == len(subs):
            return f"✅ UE 全通（{len(done)}/{len(subs)}：识别/概况/清单）"
        if not done:
            return "△ UE 只到探测"
        return f"△ UE 部分可用（缺 {', '.join(bad)}）"

    if st.get("open_game")["status"] != "ok":
        return "△ 只到探测（引擎认识，索引没建起来）"
    if st.get("search_classes")["status"] != "ok":
        return "△ 只到索引（查询没通）"
    if st.counts.get("classes_total", 0) == 0:
        return "△ 索引空（查询返回 0，静默坏结果）"
    label = {"il2cpp": "IL2CPP", "mono": "Mono"}.get(st.engine, st.engine)
    tail = []
    if st.get("class_detail")["status"] == "ok":
        tail.append("类详情")
    if st.get("decompile")["status"] == "ok":
        tail.append("反编译")
    return f"✅ {label} 可用（到{'、'.join(tail) or '类查询'}）"


async def main() -> int:
    ap = argparse.ArgumentParser(description="多游戏兼容性矩阵")
    ap.add_argument("--roots", nargs="*", default=None,
                    help="样本根目录（默认取 samples.SAMPLE_ROOTS）")
    ap.add_argument("--only", default="", help="只测名字含该子串的游戏")
    ap.add_argument("--json", dest="json_path", default=str(CACHE / "matrix.json"),
                    help="结果 JSON 落盘路径")
    ap.add_argument("--verbose", action="store_true", help="打印每一阶段的细节")
    args = ap.parse_args()

    roots = [Path(p) for p in args.roots] if args.roots else list(samples.SAMPLE_ROOTS)
    print("多游戏兼容性矩阵")
    print("=" * 100)
    print(f"样本根      : {[str(p) for p in roots]}")
    print(f"缓存目录    : {CACHE}")
    print(f"MCP 服务    : python -m {SERVER_MODULE}")
    print("-" * 100)

    cands = _dedupe_nested(discover_games(roots))
    extras, tool_dirs = discover_non_engine(roots, cands)
    if args.only:
        cands = [c for c in cands if args.only.lower() in c["install"].lower()]
        extras = [c for c in extras if args.only.lower() in c["install"].lower()]
    all_cands = cands + extras
    if not all_cands:
        print("没有发现任何候选游戏目录。")
        return 0
    print(f"发现候选 {len(all_cands)} 个"
          f"（有引擎标记 {len(cands)} + 只有大 exe {len(extras)}）")
    if tool_dirs:
        print(f"按工具目录排除 {len(set(tool_dirs))} 个："
              + "、".join(sorted({Path(p).name for p in tool_dirs})))
    print()

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", SERVER_MODULE],
        cwd=str(ROOT),
        env={
            **os.environ,
            "PYTHONPATH": str(ROOT / "src"),
            "PYTHONUTF8": "1",
            "PYTHONUNBUFFERED": "1",
            "GAMEMOD_LOG_LEVEL": "WARNING",
        },
    )

    results: list[dict] = []
    started = time.perf_counter()
    tool_count = 0

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            tool_count = len(tools.tools)
            print(f"MCP 工具数  : {tool_count}\n")

            for i, cand in enumerate(all_cands, 1):
                name = Path(cand["install"]).name
                print(f"[{i}/{len(all_cands)}] {name}")
                print(f"      {cand['install']}")
                t0 = time.perf_counter()
                st = await probe_game(session, cand, args.verbose)
                dt = time.perf_counter() - t0
                print(f"      → {reach_of(st)}   （{dt:.1f}s，引擎={st.engine}）")
                results.append({
                    "install": st.install,
                    "name": name,
                    "hint": st.hint,
                    "has_marker": cand.get("hint") != "unknown",
                    "lookalike": st.lookalike,
                    "engine": st.engine,
                    "reach": reach_of(st),
                    "elapsed_s": round(dt, 2),
                    "counts": st.counts,
                    "notes": st.notes,
                    "stages": st.stages,
                })

    total = time.perf_counter() - started

    # ------------------------------------------------------------ 矩阵
    print("\n" + "=" * 100)
    print("阶段矩阵（列 = 阶段，✓=通过  ×=失败 ·=不适用/跳过）")
    print("=" * 100)
    cols = Stages.ORDER
    head = f"{'游戏':<30}{'引擎':<8}" + "".join(f"{lab:<6}" for _, lab in cols) + "结论"
    print(head)
    print("-" * 140)
    mark = {"ok": "✓", "fail": "×", "异常": "!", "不适用": "·", "跳过": "·", "未执行": " "}
    for r in results:
        line = f"{r['name'][:29]:<30}{r['engine']:<8}"
        line += "".join(f"{mark.get(r['stages'].get(k, {}).get('status', '未执行'), '?'):<6}" for k, _ in cols)
        line += r["reach"]
        print(line)

    # ------------------------------------------------------------ 统计
    print("\n" + "=" * 100)
    print("统计")
    print("=" * 100)
    by_engine: dict[str, list[dict]] = {}
    for r in results:
        by_engine.setdefault(r["engine"], []).append(r)
    print("  按引擎（「全通」= 该引擎的**全部**能力都跑通）：")
    for eng, rs in sorted(by_engine.items()):
        ok = sum(1 for r in rs if r["reach"].startswith("✅"))
        part = sum(1 for r in rs if r["reach"].startswith("△"))
        bad = sum(1 for r in rs if r["reach"].startswith("✗"))
        print(f"    {eng:<8} 共 {len(rs):>2} 个  全通 {ok:>2}  部分 {part:>2}  未生效 {bad:>2}")

    uni = [r for r in results if r["engine"] in ("il2cpp", "mono")]
    ue = [r for r in results if r["engine"] == "ue"]
    other = [r for r in results if r["engine"] not in ("il2cpp", "mono", "ue")]
    print("\n  按「本 MCP 是否支持这个引擎」：")
    print(f"    Unity（IL2CPP/Mono） {len(uni):>2} 个，可用 "
          f"{sum(1 for r in uni if r['reach'].startswith('✅'))}")
    print(f"    Unreal              {len(ue):>2} 个，可用 "
          f"{sum(1 for r in ue if r['reach'].startswith('✅'))}")
    print(f"    其它/不认识          {len(other):>2} 个（0 个能索引 —— 本 MCP 只支持上面两类）")
    if other:
        fam = _family_counts(other)
        for k, v in sorted(fam.items(), key=lambda kv: -kv[1]):
            print(f"        · {k:<16} {v}")

    stage_fail: dict[str, list[str]] = {}
    for r in results:
        for k, lab in cols:
            if r["stages"].get(k, {}).get("status") in ("fail", "异常"):
                stage_fail.setdefault(lab, []).append(r["name"])
    print("\n  各阶段失败数（括号里是失败的游戏）：")
    for _, lab in cols:
        hits = stage_fail.get(lab, [])
        shown = "、".join(hits[:6]) + ("…" if len(hits) > 6 else "")
        print(f"    {lab:<8} {len(hits):>2}   {shown}")
    print(f"\n  总耗时 {total:.1f}s")

    # ------------------------------------------------------------ 落盘
    #
    # 统计也要落盘：矩阵 JSON 是这个报告唯一的证据文件，
    # 如果只有明细没有统计，读的人就得自己再写一个脚本汇总一遍
    # （或者更糟：凭印象引用打印输出里的数字）。
    out = Path(args.json_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "roots": [str(p) for p in roots],
        "tool_count": tool_count,
        "elapsed_s": round(total, 2),
        "stats": {
            "candidates": len(results),
            "by_engine": {
                eng: {
                    "total": len(rs),
                    "all_ok": sum(1 for r in rs if r["reach"].startswith("✅")),
                    "partial": sum(1 for r in rs if r["reach"].startswith("△")),
                    "not_working": sum(1 for r in rs if r["reach"].startswith("✗")),
                }
                for eng, rs in sorted(by_engine.items())
            },
            "supported": {
                "unity": {"total": len(uni),
                          "usable": sum(1 for r in uni
                                        if r["reach"].startswith("✅"))},
                "unreal": {"total": len(ue),
                           "usable": sum(1 for r in ue
                                         if r["reach"].startswith("✅"))},
                "other": {"total": len(other), "usable": 0,
                          "families": {
                              k: v for k, v in sorted(
                                  _family_counts(other).items(),
                                  key=lambda kv: -kv[1])
                          }},
            },
            "stage_failures": {lab: hit for lab, hit in
                               ((lab, stage_fail.get(lab, []))
                                for _, lab in cols) if hit},
            "skipped_tool_dirs": tool_dirs,
        },
        "results": results,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  明细已落盘: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
