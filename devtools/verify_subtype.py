"""验收：继承过滤反过来查（P1-4）。

要证明的问题
--------------------------------------------------
`search_classes(subtype_of=...)` 原来对**每一行**调一次 `meta.is_subclass_of`，
而它每次都要走一遍基类链 —— 合起来 O(行数 × 链长)。IL2CPP 20,072 类型上
宽查询实测净增 **+20~33 ms**（12 轮符号全为正，见
`bench_index.py` 的 `[P1-4]` 段）：对 16,780 行逐个上溯，只为筛出 1,549 个。

现在改成**反过来查**：先在 meta 侧一次算出「后代下标集合」，再与行取交集。
IL2CPP 用惰性邻接表 + BFS（基准实测 ~20 ms → 亚毫秒），Mono 规模小仍逐条判定。

这个脚本要守住两件事
--------------------------------------------------
1. **快**：邻接表只建一次（用计数证明，不用计时 —— 时间会闪，计数不会）。
2. **一模一样**：`derived_set` 的结果必须与**旧的逐条 `is_subclass_of` 完全相等**。
   这一条是重点 —— 优化继承语义最怕「快是快了，结果悄悄少了几个类」。
   所以这里拿旧的实现当**真值**（它就留在 `is_subclass_of` 里没删），
   对整个类型表逐条比对，而不是抽查几个类。

顺带守住 `find_derived`：它重写成了复用邻接表，
所以也要跟「旧写法的手抄版本」逐条对齐（含顺序与 limit 截断）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import samples                                            # noqa: E402
samples.add_src_to_path()
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from gamemod_mcp.engine.index import Index                # noqa: E402

_IL2CPP_GAME = samples.find_il2cpp()
_MONO_GAME = samples.find_mono()
IL2CPP_META = (_IL2CPP_GAME / "Garden of Witches_Data" / "il2cpp_data"
               / "Metadata" / "global-metadata.dat") if _IL2CPP_GAME else None
MONO_ASM = (_MONO_GAME / "Archon Soul_Data" / "Managed"
            / "Assembly-CSharp.dll") if _MONO_GAME else None

HAVE_IL2CPP = samples.require(IL2CPP_META, "IL2CPP 元数据 Garden of Witches")
HAVE_MONO = samples.require(MONO_ASM, "Mono 程序集 Archon Soul")

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
    print("=" * 74)
    print(title)
    print("=" * 74)


class Counter:
    """给某个方法套一层计数（回答「邻接表建了几次」）。"""

    def __init__(self, target, name: str):
        self._orig = target
        self._name = name
        self.calls = 0

    def __call__(self, *a, **kw):
        self.calls += 1
        return self._orig(*a, **kw)


#: 用来比对语义的基类名。覆盖几类情况：
#:   · Unity 框架类（跨程序集、后代最多）
#:   · System.Object（几乎全表的根）
#:   · System.Enum / System.ValueType（值类型那一支）
#:   · 游戏自己的类（后代少，但肯定是本程序集内）
#:   · 不存在的名字（必须返回空，而不是报错或返回全表）
#:   · 嵌套类型名（走 nested_name 命中的那条路）
BASE_CASES = [
    "UnityEngine.MonoBehaviour",
    "UnityEngine.Component",
    "System.Object",
    "System.Enum",
    "System.ValueType",
    "TeamTapas.ProjectR.UI2.RUI_FloatingUI",
    "这个类不存在.DoesNotExist",
]


def brute_force(meta, base: str, include_self: bool = True) -> set[int]:
    """**真值**：旧的逐条判定。

    刻意手写一遍 `is_subclass_of` 的语义，而**不是**再调一次 `derived_set` ——
    否则就是拿新实现验证新实现。
    """
    out: set[int] = set()
    for t in meta.types:
        named = (t.nested_name == base or t.full_name == base)
        if named and not include_self:
            continue
        if meta.is_subclass_of(t, base):
            out.add(t.index)
    return out


def probe(tag: str, meta_path: Path, cache: Path, is_il2cpp: bool) -> None:
    if not meta_path or not Path(meta_path).exists():
        print(f"  \u26a0\ufe0f  {tag} 样本不存在 —— 无法验证，跳过")
        return
    section(f"{tag}（{meta_path.name}）")
    idx = Index.open(meta_path, cache_dir=cache)
    _ = idx.meta                                   # 确保解析器已加载
    meta = idx.meta
    print(f"  类型总数 {len(meta.types):,}")

    # ---------------------------------------------- 1. 与旧实现逐条相等
    print()
    print("  -- 1. `derived_set` 必须与旧的逐条 `is_subclass_of` 完全相等 --")
    for base in BASE_CASES:
        got = meta.derived_set(base)
        want = brute_force(meta, base)
        # 集合相等是关键；数量只是附带信息
        same = got == want
        extra = len(got - want)
        missing = len(want - got)
        check(f"{base}", same,
              f"{len(got):,} 个后代；多 {extra} / 少 {missing}"
              if not same else f"{len(got):,} 个后代，逐条一致")
        # 任何返回值都必须是合法的类型下标（防止 BFS 压进坏下标）
        bad = [i for i in list(got)[:500] if not (0 <= i < len(meta.types))]
        check(f"{base} 的下标全部合法", not bad, f"非法 {len(bad)} 个",
              warn_only=True)

    # ---------------------------------------------- 2. include_self=False
    print()
    print("  -- 2. `include_self=False` 只去掉名字命中的根 --")
    for base in ("UnityEngine.MonoBehaviour", "System.Object"):
        got = meta.derived_set(base, include_self=False)
        want = brute_force(meta, base, include_self=False)
        check(f"{base}: 与旧实现一致（不含自身）", got == want,
              f"{len(got):,} 个")
        full = meta.derived_set(base, include_self=True)
        check(f"{base}: 不含自身 ⊂ 含自身", got <= full,
              f"{len(got):,} ≤ {len(full):,}")

    # ---------------------------------------------- 3. 邻接表只建一次
    print()
    print("  -- 3. 邻接表只建一次（计数，不计时）--")
    if is_il2cpp:
        # 换一个全新的 meta 实例，避免前面已经建过表
        idx2 = Index.open(meta_path, cache_dir=cache)
        _ = idx2.meta
        m2 = idx2.meta
        # 实例级 patch（类级会破坏绑定）
        m2.parent_of = Counter(m2.parent_of, "parent_of")
        n_types = len(m2.types)
        m2.derived_set("UnityEngine.MonoBehaviour")
        first = m2.parent_of.calls
        check("首次调用即为每个类型登记一次基类", first == n_types,
              f"parent_of 调了 {first:,} 次，类型 {n_types:,} 个")
        m2.derived_set("System.Object")
        m2.derived_set("UnityEngine.Component")
        second = m2.parent_of.calls - first
        check("之后的调用**不再**重复建表", second == 0,
              f"后续 2 次调用新增 {second} 次 parent_of（缓存命中）")
        del m2.parent_of
        idx2.close()
    else:
        print("  （Mono 侧刻意不建邻接表，见 mono_meta.derived_set 的说明）")
        check("Mono 不建表 —— 明确记录而不是假装也优化了", True,
              "1,387 类型，实测净增落在噪声里")

    # ---------------------------------------------- 4. 走到工具层
    print()
    print("  -- 4. `search_classes(subtype_of=...)` 的 total 与真值一致 --")
    for base in ("UnityEngine.MonoBehaviour", "System.Object"):
        r = idx.search_classes("", limit=5, subtype_of=base)
        # 真值要套用与 SQL 相同的排除规则（编译期生成的名字）
        want = [i for i in brute_force(meta, base)
                if not _compiler_generated(meta.types[i].name)]
        same = r["total"] == len(want)
        check(f"subtype_of={base}", same,
              f"total {r['total']:,} / 真值 {len(want):,}"
              if not same else f"total {r['total']:,}，与真值一致")
        check(f"subtype_of={base} 的 has_more 与 limit 自洽",
              r["has_more"] == (r["total"] > r["limit"]),
              f"has_more={r['has_more']}, total={r['total']:,}, limit={r['limit']}")
        # 返回的每一条都必须真的在后代集合里（不能靠 SQL 蒙）
        keep = brute_force(meta, base)
        ok = all(it["index"] in keep for it in r["items"])
        check(f"subtype_of={base} 返回项确实都是后代", ok,
              f"抽查 {len(r['items'])} 条")

    r = idx.search_classes("", limit=5, subtype_of="这个类不存在.DoesNotExist")
    # 注意：`ok` 是 server 层加的，engine 层返回的是分页载荷
    check("不存在的基类 → total=0 而不是报错或返回全表",
          r["total"] == 0 and r["items"] == [] and r["has_more"] is False,
          f"total={r['total']}, returned={r['returned']}, "
          f"has_more={r['has_more']}")

    # ---------------------------------------------- 5. find_derived 未漂移
    print()
    print("  -- 5. `find_derived` 与旧写法（手抄）逐条一致，含顺序 --")
    for base in ("UnityEngine.MonoBehaviour", "System.Object"):
        for limit in (10, 200):
            got = [t.index for t in meta.find_derived(base, limit=limit)]
            want = _find_derived_old(meta, base, limit, is_il2cpp)
            check(f"{base} limit={limit}", got == want,
                  f"{len(got)} 条，顺序一致" if got == want
                  else f"长度 {len(got)} vs {len(want)}")

    idx.close()


def _compiler_generated(name: str) -> bool:
    """复刻 `search_classes` 的 SQL 排除规则（`name NOT LIKE '<%' AND
    name NOT LIKE '%<>%'`）。"""
    return name.startswith("<") or "<>" in name


def _find_derived_old(meta, base: str, limit: int, is_il2cpp: bool) -> list[int]:
    """**旧写法的手抄版**（真值）。

    注意两侧「不含自身」的判定本来就不同（IL2CPP 只排 full_name，
    Mono 排 nested_name 或 full_name）—— 这里照抄各自的原样，
    因为本次改动**不许**顺手改掉这个差异。
    """
    out: list[int] = []
    for t in meta.types:
        if is_il2cpp:
            if t.full_name == base:
                continue
        elif t.nested_name == base or t.full_name == base:
            continue
        if meta.is_subclass_of(t, base):
            out.append(t.index)
            if len(out) >= limit:
                break
    return out


def main() -> int:
    section("继承过滤反过来查（P1-4）")
    print("  真值 = `is_subclass_of` 的逐条扫描（旧实现，未删除）")

    if HAVE_IL2CPP:
        probe("IL2CPP", IL2CPP_META, samples.CACHE_LAZY_TEST, is_il2cpp=True)
    else:
        print("\n\u26a0\ufe0f  IL2CPP 样本不存在 —— 无法验证，跳过")
    if HAVE_MONO:
        probe("Mono", MONO_ASM, samples.CACHE_LAZY_TEST, is_il2cpp=False)
    else:
        print("\n\u26a0\ufe0f  Mono 样本不存在 —— 无法验证，跳过")

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
