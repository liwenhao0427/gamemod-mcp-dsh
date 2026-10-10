"""验收：**结果句柄的作用域** —— 旧句柄不许读到另一个索引的数据（P2-4）。

## 被修的缺陷

`ResultStore` 是 `Index` 的实例属性（`index.py`），切游戏就换一个实例、
序号从 0 重来，而句柄字符串是 `f"{kind}-{n}"` —— 于是**旧会话的句柄
会在新会话里重新指向另一份结果集**。修之前实测（本脚本第 1 节复现的
就是这个场景，只是改为断言它已被拒绝）：

    IL2CPP  search_classes('Player') -> 句柄 'classes-1'，首条 PlayerPrefsController
    切 Mono  search_classes('a')     -> 句柄 'classes-1'，首条 __StaticArrayInitTypeSize=26136
    再读 IL2CPP 的句柄               -> ok=True，读出来的是 **Mono 的那一条**

`ok=True` 而数据属于另一个游戏 —— 属于「假成功」，是本项目的红线。

## 修法

句柄改成 `{引擎}-g{代次}-{kind}-{n}`，代次号来自 `ResultStore._EPOCH`
（进程内每新建一个句柄表就 +1）。唯一性由代次保证；
引擎名只是为了让人看得出句柄属于哪个会话。

## 非空转反证（第 2 节）

光断言「两个句柄不相等」是不够的 —— 句柄格式随便改改就能满足。
所以这里把新句柄的「代次段」剥掉，还原成旧格式，并断言**两者还原后相同**：
这证明「旧格式下它们确实撞名」，也就是证明第 1 节拒绝掉的正是那个真缺陷。

## 反向也要测（第 3 节）

修作用域很容易顺手把正常翻页弄坏，所以同时断言：
同一个会话内 offset / limit / pattern 三种翻页行为都还正常。
"""

from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import samples                                            # noqa: E402
samples.add_src_to_path()
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from gamemod_mcp import server as S                       # noqa: E402
from gamemod_mcp.engine.index import Index                # noqa: E402

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


def section(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


#: 句柄前缀：`{引擎}-g{代次}-`
_EPOCH_SEG = re.compile(r"-g\d+-")

#: 整个前缀（引擎 + 代次）—— 旧格式里这两段都不存在
_PREFIX = re.compile(r"^.*?-g\d+-")


def old_format(handle: str) -> str:
    """把新格式句柄还原成**旧格式**（`il2cpp-g3-classes-1` -> `classes-1`）。

    旧代码是 `handle = f"{kind}-{self._counter}"`，所以旧格式就是
    `{kind}-{n}`：引擎段与代次段都是新加的，两段都要剥掉。
    用来证明「旧格式下这两个句柄确实会撞名」。
    """
    return _PREFIX.sub("", handle)


def first_name(payload: dict) -> str:
    items = payload.get("items") or []
    if not items:
        return ""
    it = items[0]
    return str(it.get("full_name") or it.get("name") or "")


def main() -> int:
    il2cpp = samples.find_il2cpp()
    mono = samples.find_mono()
    if not samples.require(il2cpp, "IL2CPP 样本") or not samples.require(mono, "Mono 样本"):
        print("两个样本缺一不可（要跨游戏才谈得上句柄作用域）")
        return 0

    section("1. 跨游戏：旧句柄必须被拒绝，而不是读到新游戏的数据")

    opened = S.open_game(game_dir=str(il2cpp))
    if not check("open_game(IL2CPP) 成功", bool(opened.get("ok")),
                 str(opened.get("error") or "")):
        return 0
    a = S.search_classes(pattern="Player", limit=2)
    ha, a_first = a.get("handle"), first_name(a)
    check("IL2CPP 拿到句柄", bool(ha), f"句柄={ha!r} 首条={a_first}")

    opened_b = S.open_game(game_dir=str(mono))
    check("open_game(Mono) 成功", bool(opened_b.get("ok")),
          str(opened_b.get("error") or ""))
    b = S.search_classes(pattern="a", limit=2)
    hb, b_first = b.get("handle"), first_name(b)
    check("Mono 拿到句柄", bool(hb), f"句柄={hb!r} 首条={b_first}")

    # 这一条是本次修复的核心断言
    check("跨游戏的句柄字符串不同（旧格式下它们都是 'classes-1'）",
          ha != hb, f"{ha!r} vs {hb!r}")

    r = S.read_query_result(ha, limit=2)
    check("拿旧句柄读 -> 如实失败（ok=False）", r.get("ok") is False,
          f"ok={r.get('ok')} error={r.get('error')}")
    check("失败信息里带 available_handles（告诉 Agent 有哪些可用）",
          isinstance(r.get("available_handles"), list),
          f"{len(r.get('available_handles') or [])} 个可用句柄")
    check("旧句柄不在可用列表里",
          ha not in (r.get("available_handles") or []),
          f"可用={r.get('available_handles')}")

    # 最关键的一条：绝不能把 Mono 的数据当结果返回
    wrong = first_name(r)
    check("!读不到另一个游戏的数据!(这才是修掉的那个缺陷)",
          wrong != b_first,
          f"冲突数据={b_first!r}，本次读到={wrong!r}")

    section("2. 非空转反证：旧格式下这两个句柄确实会撞名")

    print(f"  新格式：{ha!r} 与 {hb!r}")
    print(f"  还原成旧格式：{old_format(ha)!r} 与 {old_format(hb)!r}")
    check("剥掉引擎段与代次段后两者相同 —— 证明旧格式真的会撞名",
          old_format(ha) == old_format(hb) and old_format(ha) != "",
          f"还原后 = {old_format(ha)!r}（旧代码就是 f\"{{kind}}-{{n}}\"）")
    check("代次段确实存在于句柄里（不是碰巧不等）",
          bool(_EPOCH_SEG.search(ha)) and bool(_EPOCH_SEG.search(hb)),
          f"匹配片段 = {_EPOCH_SEG.search(ha).group(0)!r}")
    check("代次段在两个句柄里不同",
          _EPOCH_SEG.search(ha).group(0) != _EPOCH_SEG.search(hb).group(0),
          f"{_EPOCH_SEG.search(ha).group(0)} vs {_EPOCH_SEG.search(hb).group(0)}")
    check("引擎段如实反映各自会话（IL2CPP vs Mono）",
          ha.startswith("il2cpp-") and hb.startswith("mono-"),
          f"{ha.split('-')[0]!r} vs {hb.split('-')[0]!r}")

    section("3. 反向：同一会话内的翻页/过滤必须照旧能用")

    p = S.read_query_result(hb, offset=0, limit=1)
    check("同会话内 offset/limit 翻页正常", p.get("ok") is True,
          f"ok={p.get('ok')} returned={p.get('returned')} total={p.get('total')}")
    check("翻页读回的第一条与原查询一致", first_name(p) == b_first,
          f"{first_name(p)!r} == {b_first!r}")

    p2 = S.read_query_result(hb, offset=1, limit=1)
    check("offset=1 拿到的是**不同**的一条（offset 真的生效）",
          p2.get("ok") is True and first_name(p2) != first_name(p),
          f"offset=0 -> {first_name(p)!r}，offset=1 -> {first_name(p2)!r}")

    p3 = S.read_query_result(hb, limit=3, pattern="zzz-不可能命中的串-zzz")
    check("pattern 零命中时仍 ok=True 并说明原因（契约：不许把空当不存在）",
          p3.get("ok") is True and not (p3.get("items") or []),
          f"note={str(p3.get('note'))[:52]}")

    section("4. 引擎层：同一个游戏重开索引，句柄同样不重名")

    meta = il2cpp / "Garden of Witches_Data" / "il2cpp_data" / "Metadata" / "global-metadata.dat"
    if not samples.require(meta, "IL2CPP 元数据（引擎层直开）"):
        print("  跳过（样本不同路径，无法直开）")
    else:
        i1 = Index.open(meta)
        i2 = Index.open(meta)
        try:
            check("两个 Index 实例的句柄前缀不同",
                  i1.results.prefix != i2.results.prefix,
                  f"{i1.results.prefix!r} vs {i2.results.prefix!r}")
            r1 = i1.search_classes("Player", limit=1)
            check("同一个游戏重开后，旧句柄在新实例里查不到",
                  i2.read_query_result(r1["handle"]).get("found") is False,
                  f"旧句柄={r1['handle']!r}")
            check("前缀里带引擎名（可读性：看得出句柄属于哪个引擎）",
                  i1.results.prefix.startswith("il2cpp-g"),
                  f"prefix={i1.results.prefix!r}")
        finally:
            i1.close()
            i2.close()

    S.close_session(close_runtime=False)

    print()
    print("=" * 78)
    if FAIL:
        print(f"\u274c 通过 {PASS} 项，失败 {FAIL} 项（警告 {WARN}）")
    else:
        print(f"\u2705 通过 {PASS} 项，失败 0 项"
              + (f"（警告 {WARN}）" if WARN else ""))
    print("=" * 78)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
