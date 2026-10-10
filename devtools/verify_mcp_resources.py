"""验收：**MCP 资源层**（P2-2）—— `gamemod://guide` 注册、真值、诚实性。

## 为什么只做一个资源、且不做 prompts

P2-2 原本的计划是「补 prompts + 三个资源」。动手前先量了一下客户端（DSH）
的实际情况，结论只做**一个**：

| 协议面 | 客户端实测 | 结论 |
|---|---|---|
| `resources/list` / `resources/read` | 客户端实现了；且 `dsh-mcp-resources` 把资源包成**给模型用的工具**，模型确实够得着 | ✅ 做 |
| `prompts/list` / `prompts/get` | 客户端**一个 prompt 方法都没有**，也没有 `dsh-mcp-prompts` 这类包 | ❌ 不做（加了也传不到模型面前，同 P2-1 的结论方式）|

（证据：在 `E:\\DSH\\resources\\app.asar` 里按路径检索 JS，
`dsh-mcp-client` 的请求分发 `switch` 里有 `resources/list`、
`resources/templates/list`、`resources/read` 三个分支，
`prompts/*` **零命中**。）

资源也只放**工具拿不到**的东西：

- `gamemod://capabilities` —— **不做**。`get_session().index.capabilities`
  与 `open_game` 的返回里已经有了，再开一个出口就是同一份数据的第二个来源
  （违反 AGENTS.md §3.1 第 3 条）。
- `gamemod://session` —— **不做**，理由同上（`get_session` 就是它）。
- `gamemod://guide` —— **做**。全项目唯一「没有任何工具出口」的内容：
  这份 MCP 自己的使用指南（与 `skills/gamemod-mod/SKILL.md` 同源）。

第 4 节把「资源清单恰好只有 guide」做成断言：以后谁再补一个重复资源，
这里会直接报红，逼一次讨论。

## 真值来源

第 2 节不拿「自己写的期望值」当真值，而是拿**磁盘上的
`skills/gamemod-mod/SKILL.md`** 与资源正文逐字节比对 —— 资源必须与
那份文件同源，否则文档改了、资源还是旧的，没人会发现。

## 为什么要测「找不到」

`gamemod://guide` 依赖一份仓库里的文件，而 pip 安装形态下它可能不在。
那种情况下**返回空串或假装成功**都是本项目红线（§3.1 第 4 条），
所以第 3 节把候选路径换成不存在的目录，断言它如实说明找过哪里、并给出退路。

用法
============================================================
    python devtools/verify_mcp_resources.py      # 校验，返回 0/1
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import samples                                            # noqa: E402
samples.add_src_to_path()
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from gamemod_mcp import server as S                       # noqa: E402

PASS = 0
FAIL = 0
WARN = 0

#: 资源清单必须**恰好**是这一个（多一个就说明开了重复出口，见文件头）。
EXPECTED_RESOURCES = {"gamemod://guide"}

#: 资源内容的上限。资源不走 `@_cap_response`（那是工具层的外壳），
#: 所以这里单独卡一道：正文是受控静态文件，实测约 24 KB。
MAX_GUIDE_BYTES = 64 * 1024

SKILL_PATH = samples.ROOT / "skills" / "gamemod-mod" / "SKILL.md"


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


async def _read(uri: str) -> str:
    """走真实 MCP 路径读资源，返回正文文本。"""
    items = list(await S.mcp.read_resource(uri))
    if not items:
        return ""
    return items[0].content


def main() -> int:
    # ---------------------------------------------------------------- 1. 注册
    section("1. 注册：清单、元数据、以及「没有偷偷多出来的资源」")

    resources = asyncio.run(S.mcp.list_resources())
    uris = {str(r.uri) for r in resources}
    print(f"  资源清单：{sorted(uris)}")
    check("资源清单恰好只有 gamemod://guide（多一个就可能是重复出口）",
          uris == EXPECTED_RESOURCES, f"实际={sorted(uris)}")
    check("没有资源模板（list_resource_templates 为空）",
          not asyncio.run(S.mcp.list_resource_templates()),
          "若将来加了带参数模板，请在此显式表态")

    one = next((r for r in resources if str(r.uri) == "gamemod://guide"), None)
    if one is None:
        check("能取到 gamemod://guide 的定义", False)
    else:
        check("name 非空（客户端列资源时显示它）", bool(one.name), f"name={one.name!r}")
        check("title 非空", bool(one.title), f"title={one.title!r}")
        check("description 非空（模型靠它判断要不要读）",
              bool(one.description), f"desc={one.description!r}")
        check("mimeType 是 text/markdown", one.mimeType == "text/markdown",
              f"mimeType={one.mimeType!r}")
        check("uri 用了 gamemod:// 方案（与服务器名一致）",
              str(one.uri).startswith("gamemod://"), f"uri={one.uri!r}")

    # ---------------------------------------------------------------- 2. 真值
    section("2. 真值：资源正文必须与磁盘上的 SKILL.md 逐字节相同")

    text = asyncio.run(_read("gamemod://guide"))
    check("读到了内容（非空）", bool(text.strip()), f"{len(text)} 字符")

    if not SKILL_PATH.is_file():
        check("磁盘上存在 skills/gamemod-mod/SKILL.md（真值来源）", False,
              f"缺少 {SKILL_PATH}")
    else:
        disk = SKILL_PATH.read_text(encoding="utf-8")
        check("与 skills/gamemod-mod/SKILL.md **逐字节相同**（同源，不是抄了一份）",
              text == disk,
              f"资源 {len(text)} 字符 / 磁盘 {len(disk)} 字符")
        check("行数一致", len(text.splitlines()) == len(disk.splitlines()),
              f"{len(text.splitlines())} 行")

        # 内容不是占位符：把指南里必有的七步工作流工具名当锚点。
        anchors = ["open_game", "search_classes", "class_detail", "inject_process",
                   "runtime_class_info", "hook_method", "drain_events"]
        missing = [a for a in anchors if a not in text]
        check("正文确实是完整指南（七步工作流的工具名都在）", not missing,
              f"缺={missing}" if missing else f"{len(anchors)} 个锚点齐全")

    # ---------------------------------------------------------------- 3. 体积
    section("3. 体积：资源不走 256 KB 上限，所以要单独卡住")

    nbytes = len(text.encode("utf-8"))
    check(f"正文不超过 {MAX_GUIDE_BYTES // 1024} KB（受控静态文件）",
          nbytes <= MAX_GUIDE_BYTES, f"{nbytes:,} 字节")
    check("正文也远小于工具的 256 KB 上限（资源没有截断机制）",
          nbytes < 256 * 1024, f"{nbytes:,} 字节")

    # ------------------------------------------------- 4. 诚实性：找不到时
    section("4. 找不到文件时必须如实说，不许返回空串、也不许假装成功")

    real = S._guide_candidates
    missing_dir = samples.CACHE_TEST / "不存在的指南目录"
    try:
        S._guide_candidates = lambda: [missing_dir / "SKILL.md"]   # type: ignore[assignment]
        fallback = asyncio.run(_read("gamemod://guide"))
        check("仍然返回了内容（不是空串）", bool(fallback.strip()),
              f"{len(fallback)} 字符")
        check("明确说了「找不到」", "找不到" in fallback or "没有它" in fallback,
              f"首行={fallback.splitlines()[0]!r}" if fallback else "")
        check("列出了找过的位置（不糊弄）",
              "SKILL.md" in fallback and str(missing_dir) in fallback,
              f"含查找路径={'SKILL.md' in fallback}")
        check("给出了退路（不会让 Agent 卡死）",
              "get_diagnostics" in fallback and "open_game" in fallback)
        # 非空转反证：正常路径下确实读到了磁盘内容，缺失路径下**不是**那内容。
        check("非空转反证：这份兜底文本与真实指南不同",
              bool(text) and fallback != text,
              "若两者相同，说明第 2 节的比对其实没读到真东西")
    finally:
        S._guide_candidates = real      # type: ignore[assignment]

    after = asyncio.run(_read("gamemod://guide"))
    check("恢复候选路径后，读到的又是真实指南（改动没有污染全局状态）",
          after == text, f"{len(after)} 字符")

    # ------------------------------------------------- 5. 未知 uri 要报错
    section("5. 未知 uri 不许返回空内容（那会让客户端以为资源是空的）")

    try:
        empty = asyncio.run(_read("gamemod://不存在的资源"))
        check("读未知 uri 时抛错或给出明确错误", False,
              f"却成功返回了 {len(empty)} 字符")
    except Exception as exc:
        check("读未知 uri 时明确报错", True, f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------- 6. 没碰坏工具层
    section("6. 加了资源之后，工具层不受影响")

    tools = asyncio.run(S.mcp.list_tools())
    # 工具数从**唯一真值**现算（原来写死 38）。这条断言的本意是
    # 「加资源没把工具弄丢/弄多」，不是「恰好 38 个」—— 写死数值后，
    # 每次新增工具都会在这里报红一次，制造与资源无关的噪声。
    import verify_tool_inventory as _inv
    _expected = len(_inv.code_tools())
    check(f"工具数与代码一致（{_expected} 个）", len(tools) == _expected,
          f"实际 {len(tools)}")
    check("资源没有混进工具清单",
          not [t for t in tools if "guide" in t.name or "resource" in t.name],
          f"疑似={[t.name for t in tools if 'guide' in t.name]}")
    check("instructions 里提到了这个资源（否则模型不知道它存在）",
          "gamemod://guide" in (S.mcp.instructions or ""),
          "资源再有用，没人知道也等于没有")

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
