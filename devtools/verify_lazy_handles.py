"""验收：查询结果句柄的**惰性物化**（P1-3）。

要证明的问题
--------------------------------------------------
原来 `search_*` 会把命中的**全部**行都解析成 dict 塞进句柄，与 `limit`
无关 —— 宽查询下 40% 的时间花在调用方根本看不到的条目上。
现在句柄只登记「原始行 + 解析函数」，翻页时才解析这一页。

为什么用**计数**而不是计时
--------------------------------------------------
「快了 40 ms」这种证据会随机器负载闪，而且无法区分「真的少解析了」
与「这次运气好」。所以这里把解析函数包一层计数器，直接回答
**「到底解析了多少条」** —— 这个数字是确定的：

    search_classes('', limit=20)  必须恰好解析 20 条，而不是 16,780 条

计数天然非空转：如果惰性没生效，计数会等于结果集长度（上万），
断言立刻报红。所以「解析 20 条」这件事本身就是证据。

同时必须证明**语义没变**：把惰性翻页的结果，与「全量物化后自己切片」
的结果逐条比对。只快不对是不行的。
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

from gamemod_mcp.engine import adapt                      # noqa: E402
from gamemod_mcp.engine import index as _index            # noqa: E402
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
    """给某个函数套一层计数（用于回答「到底解析了多少条」）。

    这里刻意不用 mock 库：需要的是**真实调用**被真实执行，只是顺便记个数。
    """

    def __init__(self, target, name: str):
        self._orig = target
        self._name = name
        self.calls = 0

    def __call__(self, *a, **kw):
        self.calls += 1
        return self._orig(*a, **kw)

    def reset(self) -> None:
        self.calls = 0


def instrument(idx: Index) -> dict[str, Counter]:
    """把三条解析路径换成计数版。

    - 类：`Index._row_to_type`
    - 字段：`adapt.resolve_field`（字段专用类型解析）
    - 方法：`adapt.resolve_method_return`

    类这一条**打在实例上**，不是打在类上：`Counter` 是普通对象、不走
    描述符协议，若替换类属性，`self._row_to_type` 就不再自动绑定 `self`，
    调用时会少一个参数而报错。打在实例上，包住的本来就是绑定方法。
    """
    c_type = Counter(idx._row_to_type, "_row_to_type")
    idx._row_to_type = c_type                        # type: ignore[method-assign]
    c_field = Counter(adapt.resolve_field, "resolve_field")
    adapt.resolve_field = c_field                    # type: ignore[assignment]
    c_ret = Counter(adapt.resolve_method_return, "resolve_method_return")
    adapt.resolve_method_return = c_ret              # type: ignore[assignment]
    # 后两条是模块级函数、按 `adapt.xxx(...)` 查找，替换模块属性即可生效。
    return {"type": c_type, "field": c_field, "ret": c_ret}


def restore(idx: Index, counters: dict[str, Counter]) -> None:
    # 删掉实例属性即可退回类上的绑定方法（Index 没有 __slots__）
    try:
        del idx._row_to_type
    except AttributeError:
        pass
    adapt.resolve_field = counters["field"]._orig     # type: ignore[assignment]
    adapt.resolve_method_return = counters["ret"]._orig  # type: ignore[assignment]


def probe(tag: str, meta_path: Path, cache) -> None:
    """对一份元数据跑完整组断言。"""
    section(f"{tag}：惰性句柄")
    if not meta_path:
        print("  跳过：样本不存在")
        return

    idx = Index.open(meta_path, cache_dir=cache)
    counters = instrument(idx)
    try:
        # ---------------- 1. 类：limit 决定解析条数 ----------------
        counters["type"].reset()
        r = idx.search_classes("", limit=20)
        n_class = counters["type"].calls
        total = r["total"]
        print(f"  search_classes('')  结果集 {total:,} 条，返回 {r['returned']} 条，"
              f"实际解析 {n_class} 条")
        check("search_classes(limit=20) 只解析 20 条（不是整个结果集）",
              n_class == 20,
              f"解析 {n_class} 条 / 结果集 {total:,} 条")
        # 非空转：结果集确实远大于 20，否则上面那条会「碰巧」成立
        check("结果集确实远大于 limit（上一条不是碰巧成立）",
              total > 200,
              f"total={total:,}")

        # ---------------- 2. 翻页也只解析这一页 ----------------
        handle = r["handle"]
        counters["type"].reset()
        p = idx.read_query_result(handle, offset=100, limit=5)
        n_page = counters["type"].calls
        print(f"  read_query_result(offset=100, limit=5) 解析 {n_page} 条")
        check("翻页只解析这一页（5 条）", n_page == 5, f"解析 {n_page} 条")

        # ---------------- 3. 惰性翻页 == 全量物化的切片 ----------------
        # 这是「只快不对」的守门断言：语义必须与修改前完全一致。
        src = idx.results.get(handle).source
        full = src.all()                     # 全量物化（此处刻意付这个成本）
        ok_page = True
        detail = ""
        for off, lim in ((0, 5), (7, 3), (100, 5), (total - 2, 10)):
            got = idx.read_query_result(handle, offset=off, limit=lim)
            want = full[off:off + lim]
            if got["items"] != want:
                ok_page = False
                detail = f"offset={off} limit={lim} 不一致"
                break
        check("惰性翻页结果 == 全量物化后手动切片（语义未变）", ok_page, detail)

        # ---------------- 4. 边界：越界 offset ----------------
        ov = idx.read_query_result(handle, offset=total + 500, limit=5)
        check("offset 越界时 returned=0 且 has_more=False（如实回答）",
              ov["returned"] == 0 and ov["has_more"] is False,
              f"returned={ov['returned']} has_more={ov['has_more']}")
        check("越界翻页的 total 仍是结果集真值（没有被 offset 带偏）",
              ov["total"] == total, f"{ov['total']} vs {total}")

        # ---------------- 5. has_more 的边界 ----------------
        last = idx.read_query_result(handle, offset=total - 1, limit=5)
        check("取到最后一条时 has_more=False",
              last["returned"] == 1 and last["has_more"] is False,
              f"returned={last['returned']} has_more={last['has_more']}")
        mid = idx.read_query_result(handle, offset=0, limit=1)
        check("结果集 > 1 时首页 has_more=True", mid["has_more"] is True)

        # ---------------- 6. pattern 过滤：全量物化是**已知成本** ----------------
        counters["type"].reset()
        fl = idx.read_query_result(handle, offset=0, limit=3, pattern="Player")
        n_flt = counters["type"].calls
        print(f"  read_query_result(pattern='Player') 解析 {n_flt} 条"
              f"，过滤后 {fl['total']} 条")
        check("pattern 过滤必须看全部条目（行为已如实记录，不是 bug）",
              n_flt == total,
              f"解析 {n_flt} / 结果集 {total:,}")
        want_flt = [it for it in full
                    if "player" in " ".join(str(v) for v in it.values()).lower()]
        check("pattern 过滤结果 == 手工过滤（语义一致）",
              [x["index"] for x in fl["items"]]
              == [x["index"] for x in want_flt[:3]],
              f"过滤器 {fl['total']} vs 手工 {len(want_flt)}")
        check("pattern 过滤后的 total 是过滤后条数",
              fl["total"] == len(want_flt), f"{fl['total']} vs {len(want_flt)}")

        # ---------------- 7. 字段 / 方法：同为惰性 ----------------
        counters["field"].reset()
        f = idx.search_fields("a", limit=20)
        n_field = counters["field"].calls
        print(f"  search_fields('a')  结果集 {f['total']:,} 条，"
              f"实际解析 {n_field} 条")
        check("search_fields(limit=20) 只解析 20 个字段类型",
              n_field == 20, f"解析 {n_field} 条")
        check("字段结果集确实远大于 limit", f["total"] > 200, f"total={f['total']:,}")

        counters["field"].reset()
        fp = idx.read_query_result(f["handle"], offset=50, limit=4)
        check("字段翻页只解析这一页（4 条）", counters["field"].calls == 4,
              f"解析 {counters['field'].calls} 条")
        check("字段翻页有内容（不是空转）", fp["returned"] == 4,
              f"returned={fp['returned']}")

        counters["ret"].reset()
        m = idx.search_methods("a", limit=20)
        n_ret = counters["ret"].calls
        print(f"  search_methods('a')  结果集 {m['total']:,} 条，"
              f"实际解析 {n_ret} 条")
        check("search_methods(limit=20) 只解析 20 个返回值类型",
              n_ret == 20, f"解析 {n_ret} 条")
        check("方法结果集确实远大于 limit", m["total"] > 200, f"total={m['total']:,}")

        counters["ret"].reset()
        mp = idx.read_query_result(m["handle"], offset=3, limit=2)
        check("方法翻页只解析这一页（2 条）", counters["ret"].calls == 2,
              f"解析 {counters['ret'].calls} 条")
        check("方法翻页的内容与全量物化一致",
              [x["index"] for x in mp["items"]]
              == [x["index"] for x in
                  idx.results.get(m["handle"]).source.all()[3:5]],
              f"{[x['name'] for x in mp['items']]}")

        # ---------------- 8. 句柄只登记来源，不预存 dict ----------------
        qr = idx.results.get(handle)
        check("句柄背后是「行 + 解析器」而不是已解析的 dict 列表",
              isinstance(qr.source, _index._RowSource),
              type(qr.source).__name__)
        check("句柄的 total 仍是结果集真值",
              qr.total == total, f"{qr.total} vs {total}")

        # ---------------- 9. 两个句柄互不干扰 ----------------
        c1 = idx.read_query_result(r["handle"], offset=0, limit=2)
        c2 = idx.read_query_result(f["handle"], offset=0, limit=2)
        check("不同句柄的 kind 不同（类/字段不串）",
              c1["kind"] == "classes" and c2["kind"] == "fields",
              f"{c1['kind']} / {c2['kind']}")
        check("不同句柄指向不同结果", c1["items"] != c2["items"])
    finally:
        restore(idx, counters)
        idx.close()


if __name__ == "__main__":
    if HAVE_IL2CPP:
        probe("IL2CPP（20,072 类型）", IL2CPP_META, samples.CACHE_LAZY_TEST)
    else:
        print("无法验证：IL2CPP 样本不存在")
    if HAVE_MONO:
        probe("Mono（1,387 类型）", MONO_ASM, samples.CACHE_LAZY_TEST)
    else:
        print("无法验证：Mono 样本不存在")

    print()
    print("=" * 74)
    if FAIL:
        print(f"\u274c 通过 {PASS} 项，失败 {FAIL} 项"
              + (f"，警告 {WARN} 项" if WARN else ""))
        sys.exit(1)
    print(f"\u2705 通过 {PASS} 项，失败 0 项"
          + (f"（警告 {WARN} 项）" if WARN else ""))
