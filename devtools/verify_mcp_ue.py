"""Unreal 引擎的 MCP 工具层验证。

`verify_ue.py`（子 Agent 写的）验证的是 `ue.py` 模块**本身**。
本脚本验证的是另一件事：**这些能力有没有真的接到 MCP 工具层**，
也就是通用 Agent 到底调不调得到。

这一点必须单独验，因为出现过「模块能跑但工具没接」的情况：
`ue_profile` / `ue_list_entries` / `detect_ue` 这些工具一开始根本不存在，
而 `get_diagnostics` 却报 `ue.ready = true` —— 那是最糟的一种状态：
Agent 以为能用，调了就撞墙。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# sys.path 与样本路径都由 samples.py 推导/集中定义，脚本自身不写绝对路径
sys.path.insert(0, str(Path(__file__).resolve().parent))
import samples                                            # noqa: E402
samples.add_src_to_path()
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import gamemod_mcp.server as S              # noqa: E402

# 两个已实测的真实 UE 样本：根目录从 samples.py 取，派生路径用相对路径拼出来
_UE_V12 = samples.find_ue_pak_v12()
_UE_V11 = samples.find_ue_pak_v11()
_IL2CPP_GAME = samples.find_il2cpp()

SAMPLES = [
    {
        "name": "CatapultHero",
        "root": str(_UE_V12) if _UE_V12 else "",
        "exe": str(_UE_V12 / "CatapultHero" / "Binaries" / "Win64"
                   / "CatapultHero-Win64-Shipping.exe") if _UE_V12 else "",
        "pak_version": 12,
        "entry_kind": "pak",
    },
    {
        "name": "Dinoblade",
        "root": str(_UE_V11) if _UE_V11 else "",
        "exe": str(_UE_V11 / "DinobladeGame" / "Binaries" / "Win64"
                   / "DinobladeGame-Win64-Shipping.exe") if _UE_V11 else "",
        "pak_version": 11,
        "entry_kind": "utoc",
    },
]

# 负例：必须被判定为非 UE
NEGATIVES = [
    r"C:\Windows\System32\notepad.exe",
    # IL2CPP 样本（不可用时由下面的存在性判断跳过）
    str(_IL2CPP_GAME / "Garden of Witches.exe") if _IL2CPP_GAME else "",
]

# UE 样本不可用时记为「无法验证」并跳过（跳过不算失败）
samples.require(_UE_V12, "UE 样本 CatapultHero（pak v12）")
samples.require(_UE_V11, "UE 样本 Dinoblade（pak v11）")
samples.require(_IL2CPP_GAME, "IL2CPP 样本 Garden of Witches")

PASS = 0
FAIL = 0
WARN = 0


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


def section(t: str) -> None:
    print()
    print("=" * 78)
    print(t)
    print("=" * 78)


# ============================================================ 1. 工具存在性
section("1. UE 工具是否真的注册进了 MCP")

import asyncio                                                            # noqa: E402

_tools = {t.name for t in asyncio.run(S.mcp.list_tools())}
print(f"     MCP 工具总数: {len(_tools)}")
for t in ("detect_ue", "ue_profile", "ue_list_entries", "ue_script_objects",
          "ue_manifest"):
    check(f"{t} 已注册", t in _tools)

# 这正是之前踩的坑：能力没接上，诊断却报 ready
check("UE 能力已通过工具暴露（不是只写在文档里）",
      {"detect_ue", "ue_profile", "ue_list_entries"} <= _tools)


# ============================================================ 2. 诊断诚实性
section("2. get_diagnostics 对 UE 的描述是否诚实")

d = S.get_diagnostics()
check("get_diagnostics 返回 ok", d.get("ok") is True)
ue = (d.get("engines") or {}).get("ue") or {}
print(f"     ready={ue.get('ready')} parser={ue.get('parser')}")
caps = ue.get("capabilities") or {}
check("诊断里带 UE 能力清单", bool(caps), f"{len(caps)} 项")
# 关键：未实现的能力必须写出来，不能只报「ready=True」
not_impl = {k: v for k, v in caps.items() if "未实现" in str(v)}
print(f"     明确标注「未实现」的能力: {len(not_impl)} 项")
for k in sorted(not_impl):
    print(f"       · {k}")
check("诊断明确列出未实现的 UE 能力（防止 Agent 误以为能查字段偏移）",
      len(not_impl) >= 3, f"{len(not_impl)} 项")
for must in ("usmap", "fname_pool", "property_offsets"):
    check(f"  标注了 {must} 未实现", must in caps)


# ============================================================ 3. 识别
section("3. detect_ue —— 正例识别")

for sp in SAMPLES:
    if not Path(sp["exe"]).is_file():
        check(f"{sp['name']} 样本存在", False, sp["exe"], warn_only=True)
        continue
    r = S.detect_ue(exe=sp["exe"])
    check(f"{sp['name']}：detect_ue 返回 ok", r.get("ok") is True,
          str(r.get("error", ""))[:80])
    if not r.get("ok"):
        continue
    print(f"     is_ue={r.get('is_ue')} 置信度={r.get('confidence')} "
          f"project={r.get('project_name')}")
    print(f"     UE 版本={r.get('ue_version')!r}")
    if r.get("ue_version_reason"):
        print(f"     版本缺失原因: {str(r['ue_version_reason'])[:70]}")
    check(f"{sp['name']}：识别为 UE 游戏", r.get("is_ue") is True)
    check(f"{sp['name']}：置信度 > 0", (r.get("confidence") or 0) > 0,
          f"{r.get('confidence')}")
    check(f"{sp['name']}：给出证据列表",
          len(r.get("evidence") or []) > 0,
          f"{len(r.get('evidence') or [])} 条")
    # 拿不到版本时必须给原因，而不是编一个
    if not r.get("ue_version"):
        check(f"{sp['name']}：版本拿不到时给出了原因（没有编造）",
              bool(r.get("ue_version_reason")),
              str(r.get("ue_version_reason"))[:60])


section("4. detect_ue —— 负例必须被拒绝")

for neg in NEGATIVES:
    if not Path(neg).is_file():
        check(f"负例存在 {Path(neg).name}", False, neg, warn_only=True)
        continue
    r = S.detect_ue(exe=neg)
    check(f"{Path(neg).name} 未被误判为 UE",
          r.get("ok") is True and r.get("is_ue") is False,
          f"is_ue={r.get('is_ue')} conf={r.get('confidence')}")

# 不存在的文件
bad = S.detect_ue(exe=r"C:\不存在的东西_xyz.exe")
check("不存在的 exe 被拒绝", bad.get("ok") is False, bad.get("error", ""))


# ============================================================ 5. 资源容器
section("5. ue_list_entries —— 列出资源容器内容（UE 最实用的一步）")

for sp in SAMPLES:
    if not Path(sp["exe"]).is_file():
        continue
    prof = S.ue_profile(exe=sp["exe"])
    check(f"{sp['name']}：ue_profile 返回 ok", prof.get("ok") is True,
          str(prof.get("error", ""))[:80])
    if not prof.get("ok"):
        continue
    layout = (prof.get("detection") or {}).get("layout") or {}
    paks = layout.get("pak_files") or []
    utocs = layout.get("utoc_files") or []
    print(f"     pak 文件 {len(paks)} 个 / utoc 文件 {len(utocs)} 个")

    # 选一个容器检测试
    target = None
    if sp["entry_kind"] == "utoc" and utocs:
        target = utocs[0]
    elif paks:
        target = paks[0]
    if not target:
        check(f"{sp['name']}：找到可解析的容器", False, warn_only=True)
        continue

    print(f"     解析容器: {Path(target).name} "
          f"({Path(target).stat().st_size:,} B)")
    le = S.ue_list_entries(container=target, limit=10)
    check(f"{sp['name']}：ue_list_entries 返回 ok", le.get("ok") is True,
          str(le.get("error", ""))[:80])
    if not le.get("ok"):
        continue
    total = le.get("total")
    print(f"     kind={le.get('kind')} 条目总数={total} "
          f"本页={le.get('returned')} has_more={le.get('has_more')}")
    check(f"{sp['name']}：解析出条目", (total or 0) > 0, f"total={total}")
    # 字段名统一为 returned（以前叫 count，与其它列表工具不一致）
    check(f"{sp['name']}：翻页生效（只回 limit 条）",
          le.get("returned") == 10, f"returned={le.get('returned')}")
    check(f"{sp['name']}：如实标注 has_more",
          le.get("has_more") is True and "has_more" in le,
          f"has_more={le.get('has_more')}")
    check(f"{sp['name']}：同时给出 offset/limit",
          le.get("offset") == 0 and le.get("limit") == 10,
          f"offset={le.get('offset')} limit={le.get('limit')}")

    # P1-5 回归：ue_list_entries 只许把容器解析**一次**。
    # server.py 为了区分「解析失败」与「容器真的是空」，必须先调
    # read_*_index；而 list_*_entries 内部默认又会解析一遍 ——
    # 实测这占该工具耗时 48%~52%。修复方式是把 report 传进去复用。
    # 这里数调用次数（确定量），不量时间（会闪）。
    from gamemod_mcp.engine import ue as _ue_mod
    _read_name = "read_iostore_index" if sp["entry_kind"] == "utoc" else "read_pak_index"
    _orig_read = getattr(_ue_mod, _read_name)
    _read_calls = {"n": 0}

    def _counting_read(*a, **kw):
        _read_calls["n"] += 1
        return _orig_read(*a, **kw)

    setattr(_ue_mod, _read_name, _counting_read)
    try:
        le2 = S.ue_list_entries(container=target, limit=10)
    finally:
        setattr(_ue_mod, _read_name, _orig_read)
    print(f"     ue_list_entries 一次调用触发的 {_read_name} 次数 = "
          f"{_read_calls['n']}")
    check(f"{sp['name']}：ue_list_entries 只解析容器一次（P1-5：不再重复解析）",
          le2.get("ok") is True and _read_calls["n"] == 1,
          f"{_read_name} 被调 {_read_calls['n']} 次，期望 1 次")
    check(f"{sp['name']}：去掉重复解析后返回内容不变",
          {k: v for k, v in le2.items() if k != "entries"}
          == {k: v for k, v in le.items() if k != "entries"},
          "除 entries 外的字段应当完全一致")

    for e in (le.get("entries") or [])[:3]:
        p = e.get("path") if isinstance(e, dict) else e
        print(f"       · {str(p)[:100]}")

    # 过滤：用一个容器里**确实存在**的模式。
    # 不能用 .uasset —— 实测 CatapultHero 的 pak 里一个 .uasset 都没有
    # （资产在 utoc 里，pak 里是 .res/.png/.locres 这些）。
    sample_path = ""
    for e in (le.get("entries") or []):
        p0 = e.get("path") if isinstance(e, dict) else str(e)
        if p0 and "." in p0.rsplit("/", 1)[-1]:
            sample_path = p0
            break
    if sample_path:
        frag = "." + sample_path.rsplit(".", 1)[-1]
        fl = S.ue_list_entries(container=target, pattern=frag, limit=5)
        check(f"{sp['name']}：按 pattern 过滤可用（{frag!r}）",
              fl.get("ok") is True and (fl.get("matched") or 0) > 0,
              f"matched={fl.get('matched')} total={fl.get('total')}")

    # 没命中的过滤必须说清楚（而不是让 Agent 以为容器是空的）
    none = S.ue_list_entries(container=target,
                            pattern="这个模式绝对不可能命中Xyzzy", limit=5)
    check(f"{sp['name']}：无命中时说明「是过滤没中，不是容器空」",
          none.get("ok") is True and none.get("matched") == 0
          and bool(none.get("note")),
          f"note={str(none.get('note'))[:70]}")

    # 翻页偏移
    off = S.ue_list_entries(container=target, limit=5, offset=100)
    if (total or 0) > 105:
        check(f"{sp['name']}：offset 翻页可用",
              off.get("ok") is True and off.get("offset") == 100,
              f"offset={off.get('offset')} returned={off.get('returned')}")
        # 翻过头要说清楚，不能静默给空页
        far = S.ue_list_entries(container=target, limit=5,
                               offset=(total or 0) + 1000)
        check(f"{sp['name']}：offset 超出总数时给出说明",
              far.get("ok") is True and far.get("returned") == 0
              and bool(far.get("note")),
              f"note={str(far.get('note'))[:60]}")

        # 回归：offset 越过的是**过滤后**的 matched，不是 total。
        # 曾经判的是 offset >= total，于是「matched=1511 而 offset=6000
        # （6000 < 21439）」时既无说明也无 has_more，空 entries 毫无解释。
        if sample_path:
            frag2 = "." + sample_path.rsplit(".", 1)[-1]
            fl2 = S.ue_list_entries(container=target, pattern=frag2, limit=5)
            m2 = fl2.get("matched") or 0
            if 0 < m2 < (total or 0):
                mid = S.ue_list_entries(container=target, pattern=frag2, limit=5,
                                       offset=m2 + 1)
                check(f"{sp['name']}：offset 越过 matched 但未超 total 时也给说明",
                      mid.get("ok") is True and mid.get("returned") == 0
                      and bool(mid.get("note")),
                      f"matched={m2} total={total} "
                      f"note={str(mid.get('note'))[:50]}")

# 容器不存在
bad2 = S.ue_list_entries(container=r"C:\不存在.pak")
check("不存在的容器被拒绝", bad2.get("ok") is False, bad2.get("error", ""))

# 非 pak 文件当容器：必须**明确报错**，不能报成「解析成功但没条目」。
# 这是踩过的坑：list_*_entries 在解析失败时也返回空列表，
# 一度让一个 exe 被报成「容器解析成功，但里面没有任何条目」。
if Path(NEGATIVES[1]).is_file():
    nb = S.ue_list_entries(container=NEGATIVES[1])
    check("把非 pak 文件当容器时明确报错（不假装成功）",
          nb.get("ok") is False and bool(nb.get("error")),
          f"ok={nb.get('ok')} error={str(nb.get('error'))[:70]}")
    check("报错里带上失败原因（便于排查）",
          "pak magic" in str(nb.get("error", ""))
          or "magic" in str(nb.get("error", "")),
          str(nb.get("error"))[:80])


# ============================================================ 6. 清单与脚本对象
section("6. ue_manifest / ue_script_objects")

for sp in SAMPLES:
    # **先判空串**：样本缺失时 `root` 是 `""`，而 `Path("")` 等于 `Path(".")`、
    # `.is_dir()` 返回 **True** —— 于是「缺样本就跳过」失效，脚本会拿着
    # 当前目录去调 ue_manifest，报「需要提供 path 或 game_dir」，
    # 看起来像产品缺陷，实际是样本缺失（实测：失败 1 项就是这么来的）。
    if not sp["root"] or not Path(sp["root"]).is_dir():
        print(f"  ⚠️  无法验证：样本「{sp['name']}」不在本机，跳过该样本")
        WARN += 1
        continue
    mf = S.ue_manifest(game_dir=sp["root"], limit=10)
    check(f"{sp['name']}：ue_manifest 返回 ok", mf.get("ok") is True,
          str(mf.get("error", ""))[:80])
    if mf.get("ok"):
        print(f"     清单总条数={mf.get('total')} 本页={len(mf.get('files') or mf.get('entries') or [])}")
        check(f"{sp['name']}：清单有内容", (mf.get("total") or 0) > 0,
              f"total={mf.get('total')}")
        first = (mf.get("files") or mf.get("entries") or [])[:2]
        for e in first:
            print(f"       · {str(e)[:100]}")

    if Path(sp["exe"]).is_file():
        so = S.ue_script_objects(exe=sp["exe"], limit=10)
        check(f"{sp['name']}：ue_script_objects 返回 ok", so.get("ok") is True,
              str(so.get("error", ""))[:80])
        if so.get("ok"):
            print(f"     /Script/ 模块 {so.get('modules_total')} 个 "
                  f"/ 对象 {so.get('objects_total')} 个")
            check(f"{sp['name']}：提取到 /Script/ 模块",
                  (so.get("modules_total") or 0) > 0,
                  f"{so.get('modules_total')}")
            # 必须声明这是字符串证据，不是反射元数据
            check(f"{sp['name']}：声明了「字符串证据≠反射元数据」",
                  "字符串" in str(so.get("note", ""))
                  or "反射" in str(so.get("note", "")),
                  str(so.get("note"))[:70])

mf_bad = S.ue_manifest(game_dir=r"C:\Windows")
check("在非 UE 目录找清单时如实报错", mf_bad.get("ok") is False,
      str(mf_bad.get("error", ""))[:70])


# ============================================================ 7. 与 IL2CPP 互不干扰
section("7. UE 工具不应影响 IL2CPP/Mono 会话")

IL2CPP_DIR = str(_IL2CPP_GAME) if _IL2CPP_GAME else ""
_HAS_UE_V12 = _UE_V12 is not None and Path(str(_UE_V12)).is_dir()
if IL2CPP_DIR and Path(IL2CPP_DIR).is_dir() and _HAS_UE_V12:
    op = S.open_game(game_dir=IL2CPP_DIR)
    check("IL2CPP 游戏仍能正常 open_game", op.get("ok") is True,
          str(op.get("error", ""))[:70])
    check("引擎识别未被 UE 代码干扰",
          op.get("engine") == "il2cpp", f"engine={op.get('engine')}")
    sc = S.search_classes("Player", limit=5)
    check("IL2CPP 查询仍正常", sc.get("ok") is True)
    S.close_session()

    # 反过来：先开 UE（不建索引），再确认 IL2CPP 不受影响
    insp = S.inspect_game(str(_UE_V12))
    check("inspect_game 对 UE 目录识别正确",
          insp.get("engine") == "ue", f"engine={insp.get('engine')}")
    op2 = S.open_game(game_dir=IL2CPP_DIR)
    check("看过 UE 之后 IL2CPP 仍能建索引", op2.get("ok") is True,
          str(op2.get("error", ""))[:70])
    S.close_session()


# ============================================================ 汇总
section("Unreal 引擎的 MCP 工具层验证汇总")
print(f"  通过 {PASS} 项 / 失败 {FAIL} 项 / 警告 {WARN} 项")
print()
if FAIL == 0:
    print("  \u2605 UE 能力已真正接入 MCP 工具层，且能力边界在诊断中如实声明。")
else:
    print(f"  \u26a0 有 {FAIL} 项失败。")

sys.exit(1 if FAIL else 0)
