"""决策门禁：**为什么 P2-3（给长任务加 `Context` 进度与取消）不做**。

这不是行为测试 —— 没有行为可测，因为功能没做。它钉住的是
**当初否决 P2-3 所依赖的那几条事实**。任何一条变红，意味着
「该重新读一遍 `docs/交接说明.md` §3.13，按新情况重新判断」，
**而不是**「改断言让它变绿」。

## 量出来的四条

| 协议面 | 客户端（DSH）实测 | 服务器这一侧为什么也够不着 | 结论 |
|---|---|---|---|
| `notifications/progress` | `dsh-mcp-client` 调工具时只传 `{signal, timeout, toolDefinition}`，**从不传 `onprogress`** → 请求里没有 `progressToken` | 无 token 时 `Context.report_progress` 直接 `return`（第 2 节实测：**0 条通知**）；且它是 `async`，同步工具根本调不了 | ❌ 不做 |
| `notifications/cancelled` | 客户端**确实**会传 `AbortSignal`，协议面通 | 但 `RequestContext` **没有任何取消字段**，`Context` 也没有 `cancelled`；更根本的是 FastMCP 对同步工具是**内联直接调用**（`return fn(...)`，不开线程、不 await）→ 事件循环被占满，连那条取消通知都读不到 | ❌ 不做 |
| `notifications/message`（MCP logging） | 同样只有通用 SDK 里有，**没有任何 `dsh-*` 包消费** | —— | ❌ 不做 |
| tasks 扩展（`taskSupport`） | `dsh-mcp-client` 的桥**直接抛错**：`Tool "x" requires task-based execution, which this bridge does not support` | 我们的 38 个工具都没声明 `execution` | ❌ 不做（声明 `required` 会**主动弄坏**） |

> 四条合起来是同一个结论：**协议支持 ≠ 有人消费**，
> 与 P2-1（`outputSchema`）、P2-2（`prompts`）是同一套判断方式。

## 客户端证据为什么只能写在文档里

那四条「DSH 实测」是在 `E:\\DSH\\resources\\app.asar` 里检索 JS 得到的，
而 asar 是**客户端安装目录**、不是本仓库的东西 —— 本脚本无法、也不该
依赖它。所以第 4 节只陈述 SDK 这一侧的事实，客户端那一侧留在文档里。

用法
============================================================
    python devtools/verify_mcp_context.py      # 校验，返回 0/1
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import samples                                            # noqa: E402
samples.add_src_to_path()
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from mcp.server.fastmcp import Context                     # noqa: E402
from mcp.shared.context import RequestContext              # noqa: E402
import mcp.types as mcp_types                              # noqa: E402

from gamemod_mcp import server as S                        # noqa: E402

PASS = 0
FAIL = 0
WARN = 0

#: 工具数**不再写死**，而是从唯一真值（server.py 的 @mcp.tool）现算。
#:
#: 原来这里是 `EXPECTED_TOOLS = 38`。数值写死的清单必然漂移 ——
#: 实测：新增 `set_field_value` 之后，本脚本报「工具数仍是 38 — 实际 39」，
#: 而这条报红**并不代表 P2-3 的结论变了**（新工具同样是同步、同样不碰
#: Context）。但它的提示语写着「请先读 §3.13 重新判断，不要直接改断言」，
#: 于是一次纯粹的加工具就被迫做一轮「重新判断」，容易被误当成结论失效。
#: 真正要钉住的是**下面第 2/3 节的性质**（没有人吃 ctx / 全是同步函数），
#: 那才是 §3.13 的依据；工具总数只是防漂移的哨兵，交给真值来源即可。
def _expected_tools() -> int:
    import sys as _s
    _s.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
    import verify_tool_inventory as _inv
    return len(_inv.code_tools())


EXPECTED_TOOLS = _expected_tools()

#: `RequestContext` 上**允许**出现的字段名（mcp 1.26.0 实测）。
#: 这里不是「白名单」而是**观测记录**：哪天多出 `cancelled` 之类的字段，
#: 断言会报红，那正是「可以重新考虑 P2-3」的信号。
KNOWN_REQUEST_CONTEXT_FIELDS = {
    "request_id", "meta", "session", "lifespan_context", "experimental",
    "request", "close_sse_stream", "close_standalone_sse_stream",
}


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


class FakeSession:
    """只记下「服务器往外发了几条通知」，用来证明 no-op 不是「函数坏了」。"""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_progress_notification(self, **kw) -> None:
        self.sent.append(kw)


def _ctx_with_token(token: str | None) -> tuple[Context, FakeSession]:
    meta = None if token is None else mcp_types.RequestParams.Meta(progressToken=token)
    sess = FakeSession()
    rctx = RequestContext(request_id=1, meta=meta, session=sess, lifespan_context=None)
    return Context(request_context=rctx), sess


def _tools_with_ctx_param() -> list[str]:
    """哪些工具吃了 `ctx` / `Context` 参数（正常必须是空列表）。"""
    found: list[str] = []
    for name, fn in sorted(S.RAW_TOOLS.items()):
        try:
            sig = inspect.signature(fn)
        except (TypeError, ValueError):          # pragma: no cover
            continue
        for p in sig.parameters.values():
            if p.name in ("ctx", "context") or p.annotation is Context:
                found.append(f"{name}.{p.name}")
    return found


def _progress_count(token: str | None) -> int:
    """在给定 progressToken 下，`report_progress` 真的发出了几条通知。"""
    ctx, sess = _ctx_with_token(token)
    asyncio.run(ctx.report_progress(50, 100, "建索引中"))
    return len(sess.sent)


def main() -> int:
    print("=" * 78)
    print("P2-3 决策门禁：为什么不给长任务加 Context 进度与取消")
    print("=" * 78)

    tools = asyncio.run(S.mcp.list_tools())

    # ------------------------------------------------- 1. 决策现状
    section("1. 决策现状：38 个工具一个都不碰 Context（这行断言是防漂移的）")

    check(f"工具数仍是 {EXPECTED_TOOLS}", len(tools) == EXPECTED_TOOLS,
          f"实际 {len(tools)}")

    ctx_params: list[str] = []
    async_tools: list[str] = []
    for name, fn in sorted(S.RAW_TOOLS.items()):
        if inspect.iscoroutinefunction(fn):
            async_tools.append(name)
    ctx_params = _tools_with_ctx_param()

    check("没有任何工具吃 ctx / Context 参数", not ctx_params,
          f"吃到的是 {ctx_params}" if ctx_params else "38 个全是纯参数工具")
    check("38 个工具全是同步函数（这决定了第 3 节的结论）", not async_tools,
          f"异步的有 {async_tools}" if async_tools
          else "FastMCP 对同步工具走 `return fn(...)`，见第 3 节")

    src_path = os.path.join(samples.ROOT, "src", "gamemod_mcp", "server.py")
    src = open(src_path, encoding="utf-8").read()
    check("server.py 源码里连 `Context` 这个词都没有",
          "Context" not in src,
          "要重新考虑 P2-3，第一步就得先把这个词引进来")

    declared = [t.name for t in tools if getattr(t, "execution", None) is not None]
    check("38 个工具都没有声明 execution（即没有 taskSupport）", not declared,
          f"声明了的是 {declared}" if declared else "一个都没有")

    # ------------------------------------------------- 2. 进度
    section("2. 进度：没有 progressToken 时是**静默丢弃**（行为实验）")

    ctx_none, sess_none = _ctx_with_token(None)
    ret = asyncio.run(ctx_none.report_progress(50, 100, "建索引中"))
    check("无 progressToken：一条通知都不发（这就是 DSH 的实际情形）",
          len(sess_none.sent) == 0, f"却发出了 {len(sess_none.sent)} 条")
    check("无 progressToken：返回 None 且不抛异常（不假装成功、也不炸）",
          ret is None, f"返回 {ret!r}")

    # 反证：同一段代码在有 token 时必须真的发出去 ——
    # 否则「0 条」可能只是因为我把实验写坏了（函数名写错、session 没接上）。
    ctx_tok, sess_tok = _ctx_with_token("tok-反证")
    asyncio.run(ctx_tok.report_progress(50, 100, "建索引中"))
    check("有 progressToken：确实发出 1 条（**非空转反证**）",
          len(sess_tok.sent) == 1 and sess_tok.sent[0].get("progress") == 50,
          f"发出 {sess_tok.sent}")

    check("report_progress 是协程函数 → 同步工具连调都调不了（要先 await）",
          inspect.iscoroutinefunction(Context.report_progress),
          f"signature: {inspect.signature(Context.report_progress)}")

    meta = ctx_none.request_context.meta
    check("progressToken 的唯一来源是请求的 _meta（我们无法自己造一个）",
          meta is None, f"meta={meta!r}")

    # ------------------------------------------------- 3. 取消
    section("3. 取消：客户端会发，但同步工具在结构上够不着")

    check("Context 没有 `cancelled` 属性", not hasattr(Context, "cancelled"),
          "有的话就该重新评估 P2-3")
    check("Context 没有 `is_cancelled` 属性", not hasattr(Context, "is_cancelled"))

    rctx_fields = {f.name for f in dataclasses.fields(RequestContext)}
    extra = rctx_fields - KNOWN_REQUEST_CONTEXT_FIELDS
    check("RequestContext 上没有任何取消相关字段", not extra,
          f"多出来的字段：{sorted(extra)}" if extra
          else f"{len(rctx_fields)} 个字段全是非取消的：{sorted(rctx_fields)}")

    # 这条是本节的硬核：FastMCP 对同步工具是内联调用，不开线程、不 await。
    from mcp.server.fastmcp.utilities import func_metadata
    fm_src = inspect.getsource(func_metadata.FuncMetadata.call_fn_with_arg_validation)
    check("FastMCP 内联调用同步函数（`return fn(...)`，无 await、无 to_thread）",
          "return fn(**arguments_parsed_dict)" in fm_src
          and "to_thread" not in fm_src,
          "变红 = FastMCP 改成线程/异步执行了，取消与进度都值得重看一遍")

    print()
    print("  说明：同步工具在事件循环上**直接跑到底**，期间读不到任何入站消息 ——")
    print("        所以 `notifications/cancelled` 只能在工具**跑完之后**才被处理，")
    print("        那时结果已经算出来了。要让它可达，得把 open_game / launch_game /")
    print("        ue_profile 改成 async + anyio.to_thread.run_sync 包一层 ——")
    print("        那是三个工具的结构性改造，而收益（进度）恰好是客户端不消费的。")

    # ------------------------------------------------- 4. tasks 扩展
    section("4. tasks 扩展：SDK 有类型，客户端直接拒绝")

    modes = set(mcp_types.TaskExecutionMode.__args__)
    check("SDK 的 TaskExecutionMode 三个取值（记录下来，供将来对照）",
          modes == {"forbidden", "optional", "required"}, f"{sorted(modes)}")
    check("我们没有任何工具声明 execution（同 §1，这里针对 taskSupport）",
          not declared,
          "客户端对 taskSupport='required' 是**直接抛错**；\n"
          "     证据在客户端里：`requires task-based execution, which this bridge does not support`")

    # ------------------------------------------------- 5. 非空转反证
    section("5. 非空转反证：把条件反过来，上面两条断言必须真的报红")

    def _fake_with_ctx(ctx) -> dict:                  # noqa: ANN001
        return {"ok": True}

    S.RAW_TOOLS["__falsify_with_ctx"] = _fake_with_ctx
    try:
        flipped_ctx = _tools_with_ctx_param()
    finally:
        S.RAW_TOOLS.pop("__falsify_with_ctx", None)
    check("塞一个吃 ctx 的假工具后，检测器确实报得出来",
          flipped_ctx == ["__falsify_with_ctx.ctx"], f"检测到 {flipped_ctx}")
    check("移除后检测器恢复为空（说明第 1 节那两条不是恒真的）",
          not _tools_with_ctx_param())

    _orig_progress = Context.report_progress

    async def _always_send(self, progress, total=None, message=None):   # noqa: ANN001
        await self.request_context.session.send_progress_notification(
            progress_token="forced", progress=progress, total=total, message=message)

    Context.report_progress = _always_send
    try:
        flipped_n = _progress_count(None)
    finally:
        Context.report_progress = _orig_progress
    check("把 report_progress 换成「无视 token 一律发送」后，"
          "「无 token ⇒ 0 条」确实不再成立",
          flipped_n == 1, f"仍是 {flipped_n} 条 —— 说明第 2 节那行是空转的")
    check("复原后又回到 0 条（没有污染全局状态）", _progress_count(None) == 0)

    print()
    print("=" * 78)
    if FAIL:
        print(f"\u274c 通过 {PASS} 项，失败 {FAIL} 项（警告 {WARN}）")
        print("   失败意味着「否决 P2-3 的依据变了」，请先读 docs/交接说明.md §3.13，")
        print("   按新情况重新判断，**不要**直接改断言。")
    else:
        print(f"\u2705 通过 {PASS} 项，失败 0 项"
              + (f"（警告 {WARN}）" if WARN else ""))
        print("   结论：P2-3 的否决依据仍然成立（进度无消费方、取消够不着、tasks 会弄坏）。")
    print("=" * 78)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
