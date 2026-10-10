"""验收：**响应体积上限的机制**（P2-4）—— 截断、句柄、字段诚实性、不丢数据。

`verify_response_size.py` 回答的是「现在最大的返回有多大」；
本脚本回答的是「那个上限**怎么起作用**、有没有副作用」，所以两层都测：

- **纯函数层**（不需要游戏）：把 `cap.enforce` 当成一个可穷举的函数来测 ——
  没超限时是不是**原样返回**、截断后 `has_more` / `returned` 有没有说实话、
  复用已有句柄时续读的 `offset` 算得对不对、没有句柄表时会不会**假装**有、
  不是列表的超大字段会不会被**悄悄丢掉**。
- **端到端层**（需要 IL2CPP 样本）：用真实工具跑，证明
  「被截断返回的前 N 条 == 未截断时的前 N 条」（不是重排/重新编号），
  且完整列表**一条不少**地留在句柄里。

## 两个容易想当然、实际踩过的点

1. **续读的 offset 不是 `keep`。** 复用已有句柄时，`items` 是
   `offset .. offset+limit` 这一段，所以要续的是 `offset + keep`；
   按 `keep` 算会**静默跳过**前面那些条（第 4 节专门钉这个）。
2. **量探针时还没有 `note`。** 截断之后才写的 `capped` / `note` 也占字节，
   不留余量就会超出上限（实测超出 160 B）。`cap.RESERVE_BYTES` 就是为它留的。
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

from gamemod_mcp import cap, server as S                  # noqa: E402
from gamemod_mcp.engine.index import ResultStore          # noqa: E402

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


def rows(n: int, width: int = 200) -> list[dict]:
    """造 n 条「像类/字段那样」的记录，宽度可控。"""
    return [{"index": i, "name": f"Type{i}", "pad": "x" * width} for i in range(n)]


# ============================================================ 纯函数层


def test_pure() -> None:
    section("1. 没超限时必须原样返回（连字典都不复制）")
    small = {"ok": True, "items": [{"i": 1}], "total": 1}
    out = cap.enforce(small, tool="t", store=None)
    check("未超限时返回的是**同一个对象**（零开销快路径）", out is small,
          f"measure={cap.measure(small):,} B，上限={cap.CAP_BYTES:,} B")
    check("未超限时不添加 capped 字段", "capped" not in out and "note" not in out)

    section("2. 超限 + 没有句柄：截断、登记句柄、且**一条不丢**")
    full = rows(4000)
    store = ResultStore(owner="t")
    out = cap.enforce({"ok": True, "items": full, "total": 4000,
                       "returned": 4000, "has_more": False},
                      tool="t", store=store)
    kept = out.get("items") or []
    check("截断后 measure 不超过上限", cap.measure(out) <= cap.CAP_BYTES,
          f"{cap.measure(out):,} B <= {cap.CAP_BYTES:,} B")
    check("确实截断了（保留条数 < 原条数）", 0 < len(kept) < len(full),
          f"保留 {len(kept)} / {len(full)} 条")
    check("如实标了 capped + cap_bytes",
          out.get("capped") is True and out.get("cap_bytes") == cap.CAP_BYTES)
    check("给了句柄", bool(out.get("handle")), f"handle={out.get('handle')!r}")
    check("句柄确实在句柄表里",
          out.get("handle") in store.handles(),
          f"表里共 {len(store.handles())} 个")
    qr = store.get(out.get("handle", ""))
    check("!!完整列表一条不少地留在句柄里!!",
          qr is not None and qr.source.all() == full,
          f"句柄内 {0 if qr is None else len(qr.source.all())} 条，"
          f"原 {len(full)} 条")
    check("note 写清了怎么取剩下的",
          "read_query_result" in (out.get("note") or "")
          and str(len(kept)) in (out.get("note") or ""),
          f"note={str(out.get('note'))[:70]}...")
    check("has_more 被改成 True（明明还有却说没有就是撒谎）",
          out.get("has_more") is True, f"原值 False -> {out.get('has_more')}")
    check("returned 改成实际保留条数，不再是原来的 4000",
          out.get("returned") == len(kept),
          f"returned={out.get('returned')}，实际 {len(kept)}")

    section("3. 超限 + 已有句柄：**不许**另造一个句柄")
    # 这是 search_classes 的形状：_paged 已经给了指向完整结果集的句柄
    paged = {"ok": True, "items": rows(4000), "handle": "il2cpp-g1-classes-9",
             "total": 9000, "returned": 4000, "has_more": False}
    out2 = cap.enforce(paged, tool="search_classes", store=ResultStore(owner="t"))
    check("句柄没变（复用原来那个）", out2.get("handle") == "il2cpp-g1-classes-9",
          f"{paged['handle']!r} -> {out2.get('handle')!r}")
    check("没有把新句柄塞进句柄表",
          "il2cpp-g1-classes-9" not in
          (out2.get("available_handles") or []),
          "（用的是复用路径，不应登记新句柄）")

    section("4. 复用句柄时续读 offset = 原 offset + keep（不是 keep）")
    keep = len(out2.get("items") or [])
    note = out2.get("note") or ""
    m = re.search(r"offset=(\d+)", note)
    got = int(m.group(1)) if m else -1
    check("note 里的 offset == 原 offset(0) + keep",
          got == 0 + keep, f"note 说 offset={got}，应为 {keep}")
    # 非零起始 offset 才是真正会踩坑的情形
    paged2 = {"ok": True, "items": rows(4000), "handle": "h", "offset": 1000,
              "limit": 4000, "total": 9000, "returned": 4000}
    out3 = cap.enforce(paged2, tool="search_classes", store=ResultStore(owner="t"))
    keep3 = len(out3.get("items") or [])
    m3 = re.search(r"offset=(\d+)", out3.get("note") or "")
    got3 = int(m3.group(1)) if m3 else -1
    check("offset=1000 起始时，续读 offset == 1000 + keep",
          got3 == 1000 + keep3,
          f"note 说 offset={got3}，应为 {1000 + keep3}（按 keep 算会跳过 1000 条）")
    check("payload 顶层的 offset 字段保持调用方原来的请求值",
          out3.get("offset") == 1000, f"offset={out3.get('offset')}")

    section("5. 没有句柄表时：截断 + **如实说取不到**，不许假装有句柄")
    out4 = cap.enforce({"ok": True, "items": rows(4000)}, tool="t", store=None)
    check("没有凭空造出 handle", not out4.get("handle"),
          f"handle={out4.get('handle')!r}")
    check("note 明确说了剩下的取不到（而不是沉默）",
          "取不到" in (out4.get("note") or ""),
          f"note={str(out4.get('note'))[:60]}...")

    section("6. 体积不在列表里：不截数据，但必须如实标 oversize")
    blob = {"ok": True, "source": "z" * 400_000}
    out5 = cap.enforce(blob, tool="decompile", store=ResultStore(owner="t"))
    check("标了 oversize", out5.get("oversize") is True)
    check("超长字段**一个字节都没被截**（宁可超限也不悄悄丢数据）",
          out5.get("source") == blob["source"],
          f"长度 {len(out5.get('source') or '')} == {len(blob['source'])}")
    check("note 指向该工具并让人缩小范围",
          "decompile" in (out5.get("note") or "")
          and "缩小" in (out5.get("note") or ""),
          f"note={str(out5.get('note'))[:70]}...")

    section("7. 边界与形态")
    check("失败响应也会被量（规则是「任何返回」）",
          cap.enforce({"ok": False, "items": rows(4000), "error": "x"},
                      tool="t", store=None).get("capped") is True)
    check("非 dict 原样返回", cap.enforce("hello", tool="t") == "hello")
    check("空 dict 原样返回", cap.enforce({}, tool="t") == {})
    check("measure 是单调的（条数多则更大）",
          cap.measure({"items": rows(10)}) < cap.measure({"items": rows(2000)}),
          f"{cap.measure({'items': rows(10)}):,} < "
          f"{cap.measure({'items': rows(2000)}):,}")
    check("超限时不管 ok 是真是假都截",
          len(cap.enforce({"ok": False, "items": rows(4000)}, tool="t",
                          store=None).get("items") or []) < 4000)

    section("8. 非空转反证：把上限调大，同一个载荷就不再被截")
    saved = cap.CAP_BYTES
    try:
        cap.CAP_BYTES = 100 * 1024 * 1024
        out6 = cap.enforce({"ok": True, "items": rows(4000), "total": 4000},
                           tool="t", store=ResultStore(owner="t"))
        check("上限调大后不再截断（证明截断确实是上限造成的）",
              "capped" not in out6 and len(out6.get("items") or []) == 4000,
              f"capped={out6.get('capped')} 条数={len(out6.get('items') or [])}")
        cap.CAP_BYTES = 4000        # 调到极小，看它是否照样工作
        out7 = cap.enforce({"ok": True, "items": rows(4000), "total": 4000},
                           tool="t", store=None)
        check("上限调到 4 KB 时连小载荷也截（不是碰巧只对某个尺寸有效）",
              out7.get("capped") is True and cap.measure(out7) <= 4000,
              f"measure={cap.measure(out7):,} 条数={len(out7.get('items') or [])}")
    finally:
        cap.CAP_BYTES = saved

    section("8b. 上限检查**自己**出错时，不许连累工具（附加保护不能变成功能故障）")
    circular = {"ok": True, "note": "循环引用没法序列化"}
    circular["self"] = circular
    # 注意：`_cap_response` 有**副作用** —— 它会把原函数登记进 `RAW_TOOLS`
    # （那是给「量截断前有多大」用的）。这里传的是临时 lambda，
    # 登记进去就会让第 13 节「RAW_TOOLS 恰好等于 38 个工具」多出一条。
    # 本轮就是这么被第 13 节抓到的（**套件里**才红，单独跑不红）。
    raw_snapshot = dict(S.RAW_TOOLS)
    try:
        guarded = S._cap_response(lambda: circular)
        got_c = guarded()
        check("量不出来的载荷：工具照原样返回，不抛异常", got_c is circular,
              "（json.dumps 抛 ValueError，被 wrapper 兜住并打到 stderr）")
        ok_norm = S._cap_response(lambda: {"ok": True, "items": rows(4000)})()
        check("兜底没有把正常路径一起放掉（同一 wrapper 仍会截断）",
              ok_norm.get("capped") is True,
              f"条数={len(ok_norm.get('items') or [])}")
    finally:
        S.RAW_TOOLS.clear()
        S.RAW_TOOLS.update(raw_snapshot)


# ============================================================ 端到端层


def test_e2e() -> None:
    game = samples.find_il2cpp()
    if not game:
        print()
        print("（无 IL2CPP 样本：端到端一节记为「无法验证」并跳过）")
        return
    opened = S.open_game(game_dir=str(game))
    if not check("open_game 成功", bool(opened.get("ok")),
                 str(opened.get("error") or "")):
        return

    section("9. 端到端：真实工具被截断后，前 N 条必须与未截断时**逐字节相同**")
    # 用 `S.RAW_TOOLS[...]` 而不是 `S.search_classes.__wrapped__`：
    # 动态覆盖率垫片会把模块属性换成它自己的记录包装器（没有 `__wrapped__`），
    # 于是「单独跑绿、进套件红」。原因见 `server.py` 里 `RAW_TOOLS` 的注释。
    raw_fn = S.RAW_TOOLS.get("search_classes")
    check("RAW_TOOLS 里有未套上限的原函数（量「截断前」的基础）",
          callable(raw_fn), f"共登记 {len(S.RAW_TOOLS)} 个工具")

    cap_store = S.state.state().cap_results
    cap_before = list(cap_store.handles())
    raw = raw_fn(pattern="", limit=2000)
    got = S.search_classes(pattern="", limit=2000)
    check("search_classes('', 2000) 确实被截断了",
          got.get("capped") is True and len(got.get("items") or []) < len(raw["items"]),
          f"{len(raw['items'])} 条（{cap.measure(raw):,} B）-> "
          f"{len(got.get('items') or [])} 条（{cap.measure(got):,} B）")
    check("截断后的 items == 未截断 items 的前缀（没有重排/重新编号）",
          got.get("items") == raw["items"][:len(got.get("items") or [])],
          f"比对 {len(got.get('items') or [])} 条")
    # 注意：不能断言两个 handle 字符串相等 —— 每次调用都会登记一个新句柄
    # （`_paged` 无条件 `put`），所以该断言本来就是错的。真正要钉的是
    # 「用的是**索引**句柄，没有被换成 cap 句柄」。
    check("句柄仍是索引句柄（il2cpp- 前缀），没被换成 cap 句柄",
          str(got.get("handle") or "").startswith("il2cpp-g")
          and "cap-" not in str(got.get("handle")),
          f"handle={got.get('handle')!r}")
    check("cap 句柄表没有因此多出条目（走了复用路径，不新造句柄）",
          list(cap_store.handles()) == cap_before,
          f"{cap_before} -> {list(cap_store.handles())}")

    n = len(got.get("items") or [])
    nxt = S.read_query_result(got["handle"], offset=n, limit=1)
    check("按 note 里的 offset 续读，拿到的正是**紧接着**的那一条",
          nxt.get("ok") is True
          and (nxt.get("items") or [{}])[0] == raw["items"][n],
          f"第 {n} 条 = {str((nxt.get('items') or [{}])[0])[:60]}")

    section("10. 端到端：没有自带句柄的工具，完整列表进 cap 句柄")
    raw2 = S.RAW_TOOLS["find_derived_classes"](
        base_full_name="UnityEngine.MonoBehaviour", limit=2000)
    got2 = S.find_derived_classes(base_full_name="UnityEngine.MonoBehaviour",
                                  limit=2000)
    check("find_derived_classes(2000) 被截断",
          got2.get("capped") is True,
          f"{len(raw2.get('items') or [])} 条（{cap.measure(raw2):,} B）-> "
          f"{len(got2.get('items') or [])} 条（{cap.measure(got2):,} B）")
    h2 = got2.get("handle") or ""
    check("新句柄是 cap 会话级的（前缀 cap-）", h2.startswith("cap-g"),
          f"handle={h2!r}")

    # 分页取回：**不能用 limit=2000 一把取** —— 那样这一条响应自己就超限、
    # 会被 cap 再截一次（`read_query_result` 的返回同样受上限管，
    # 只是它自带 handle 所以走复用路径，不会丢数据）。
    # 顺带这也证明「被截断的响应 + 分页」这条链路是通的。
    full_raw = raw2.get("items") or []
    got_rows: list[dict] = []
    pages = 0
    while len(got_rows) < len(full_raw) and pages < 20:
        page = S.read_query_result(h2, offset=len(got_rows), limit=500)
        if not page.get("ok"):
            check("分页读取成功", False, str(page.get("error"))[:60])
            break
        chunk = page.get("items") or []
        if not chunk:
            break
        if page.get("capped"):
            pages += 1          # 兜底：万一 500 条也超限
        got_rows += chunk
        pages += 1
    check("!!分页取回的完整列表与未截断时一模一样（一条不丢、顺序一致）!!",
          got_rows == full_raw,
          f"经句柄取回 {len(got_rows)} 条，原 {len(full_raw)} 条，翻了 {pages} 页")

    # read_query_result 自己的大页也会被截：必须仍能续读，且用同一个句柄
    big_page = S.read_query_result(h2, offset=0, limit=2000)
    if big_page.get("capped"):
        n_big = len(big_page.get("items") or [])
        check("read_query_result 的一页超限时，复用同一个句柄（不新造）",
              big_page.get("handle") == h2,
              f"handle={big_page.get('handle')!r} == {h2!r}")
        check("并把剩下的条数如实交代（note 里有 offset）",
              f"offset={n_big}" in (big_page.get("note") or ""),
              f"只回了 {n_big} 条；note={str(big_page.get('note'))[:60]}...")
        tail = S.read_query_result(h2, offset=n_big, limit=200)
        check("接着上一页续读拿到的是紧接着的那些条（不缺不重）",
              (tail.get("items") or []) == full_raw[n_big:n_big + 200],
              f"续读 {len(tail.get('items') or [])} 条")
    else:
        check("read_query_result(limit=2000) 这一页未超限（本条不断言截断）",
              "capped" not in big_page,
              f"measure={cap.measure(big_page):,} B")

    section("11. 未超限的工具不许被加上任何 cap 痕迹")
    sess = S.get_session()
    check("get_session 没有 capped / handle / oversize 痕迹",
          not ({"capped", "oversize"} & set(sess.keys())),
          f"键={sorted(sess.keys())[:6]}...")
    small = S.search_classes(pattern="Player", limit=5)
    check("小查询（limit=5）原样返回，不带 capped",
          "capped" not in small, f"returned={small.get('returned')}")

    section("12. 端到端：class_hierarchy 的 down_has_more 也说实话")
    got3 = S.class_hierarchy(full_name="UnityEngine.MonoBehaviour",
                             direction="down", depth=6)
    if got3.get("capped"):
        check("down 被截断时 down_has_more 为 True（字段名带前缀也要管）",
              got3.get("down_has_more") is True,
              f"down={len(got3.get('down') or [])} 条 "
              f"down_has_more={got3.get('down_has_more')}")
    else:
        check("class_hierarchy 未超限时保持原样（本条不断言截断）",
              "capped" not in got3,
              f"measure={cap.measure(got3):,} B <= 上限 {cap.CAP_BYTES:,} B")

    S.close_session(close_runtime=False)


# ============================================================ 装饰器本身


def test_decorator() -> None:
    """`@_cap_response` 是**加在 38 个工具上的**，所以它自己也得被验收。

    尤其是「不能改变入参 schema」：用 `*args, **kwargs` 包一层如果忘了
    `functools.wraps`，FastMCP 就会把包装器的签名当成工具签名 ——
    现象是工具的入参**全部消失**（或者多出 `args` / `kwargs`），
    而工具**照样能跑**，所以只有比对 schema 才看得出来。

    本轮 P2-4 加装饰器时就是这么验的（用 `_dump_schemas.py` 把 38 个工具的
    schema 落盘、改前改后比 sha256）。这里把它变成**常驻断言**，
    免得下次再加装饰器时又要靠一次性脚本。
    """
    section("13. 装饰器不许改变入参 schema（functools.wraps 真的保住了签名）")
    import asyncio
    import inspect

    tools = {t.name: t for t in asyncio.run(S.mcp.list_tools())}
    # 工具数从**唯一真值**（server.py 的 @mcp.tool）现算，不写死数值。
    # 写死的清单必然漂移：实测新增 `set_field_value` 后这里报红，
    # 而那条断言的**本意**是「装饰器没把工具弄丢」，不是「恰好 38 个」。
    import verify_tool_inventory as _inv
    expected = len(_inv.code_tools())
    check("工具数与 server 模块里的工具数一致", len(tools) == expected,
          f"list_tools 给出 {len(tools)} 个，代码里 {expected} 个")

    missing, mismatch, varargs = [], [], []
    for name, t in sorted(tools.items()):
        orig = S.RAW_TOOLS.get(name)
        if orig is None:
            missing.append(name)
            continue
        props = set((t.inputSchema or {}).get("properties") or {})
        sig = inspect.signature(orig)
        params = set(sig.parameters)
        if props != params:
            mismatch.append(f"{name}: schema{sorted(props)} != 签名{sorted(params)}")
        # 注意别把 `launch_game(args: str)` 误判成泄漏 —— 那个 `args`
        # 是**真的要收**命令行参数，是 POSITIONAL_OR_KEYWORD。
        # 要拦的是**原函数**用了 *args / **kwargs：那样 FastMCP 生成的
        # schema 必然不完整（参数名都会变成 args / kwargs）。
        var = [p.name for p in sig.parameters.values()
               if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD)]
        if var:
            varargs.append(f"{name}: {var}")

    check("每个工具都登记了未套上限的原函数（RAW_TOOLS 全覆盖）",
          not missing and len(S.RAW_TOOLS) == len(tools),
          f"缺失：{missing}" if missing
          else f"RAW_TOOLS 共 {len(S.RAW_TOOLS)} 个 / 工具 {len(tools)} 个")
    check("每个工具注册出去的入参 schema 与其原函数签名完全一致",
          not mismatch,
          f"不一致：{mismatch[:3]}" if mismatch else "38 个工具逐个比对通过")
    check("没有任何工具用 *args / **kwargs 收参数（那样 schema 必然不完整）",
          not varargs,
          f"发现：{varargs}" if varargs
          else "38 个工具的参数都是显式声明的（`launch_game(args=...)` 的 args "
               "是普通参数，不算）")

    # 反证：上一条「schema 与签名一致」必须证明它**能**失败 ——
    # 去掉 functools.wraps 包一层，签名立刻就变成 (*args, **kwargs)。
    def naive_wrap(fn):
        def w(*args, **kwargs):
            return fn(*args, **kwargs)
        return w

    sample = S.RAW_TOOLS["search_classes"]
    naive_sig = set(inspect.signature(naive_wrap(sample)).parameters)
    check("反证：去掉 functools.wraps 后签名只剩 (*args, **kwargs)（证明上一条非空转）",
          naive_sig == {"args", "kwargs"},
          f"naive 包装后签名={sorted(naive_sig)}，"
          f"正确包装后={sorted(inspect.signature(sample).parameters)}")


def main() -> int:
    test_pure()
    test_e2e()
    test_decorator()
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
