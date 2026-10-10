#!/usr/bin/env python3
"""从代码**生成** README 的「工具一览」表格 —— 让文档不可能漂移。

为什么要生成而不是校验
============================================================
`verify_tool_inventory.py` 是**校验**：它能在漂移发生后报红。
但它管不住「漂移发生」这件事 —— 实测 README 的工具数已漂过 5 次以上
（AGENTS.md §4 里逐条记着）。校验只能*发现*问题，生成**根本不会产生**问题。

这一条是参考 `zhaoxuya520/reverse-skill`（38.4k★）的做法学来的：
它的 `skills/INDEX.md` 由 `extract-summaries.ps1` 从 frontmatter 生成，
并有 drift check。**生成 > 校验** —— 这是本轮唯一一处「对外部项目的直接采纳」。

单一来源是哪一份
============================================================
**工具函数的 docstring 首段**。为什么不是另造一份元数据表：
那会变成第二个真值源，而「手工维护的清单必然漂移」正是本项目反复踩的坑。

为什么必须先做「知识上移」
============================================================
生成前实测过：README 单元格里有 **402 个特征词**是 docstring 里没有的
（例如 `class_hierarchy` 的「早期版本把传递式后代当成直接子类」）。
直接按 docstring 生成会**丢掉这些手写知识** —— 那不是改进，是倒退。
所以先把这些事实补进 docstring 首段（那才是 Agent 真正读到的地方），
再让 README 从 docstring 生成。**信息只有一份来源，且是 Agent 能看见的那份。**

层级从哪来
============================================================
解析 `server.py` 里 `# N. 层名` 的节注释（结构性，不与代码重复表述）。
实测 44/44 个工具都能归位，且与 MCP 注册表完全对账。

用法
============================================================
    python devtools/gen_docs.py            # 打印将要写入的表格（不落盘）
    python devtools/gen_docs.py --write    # 写回 README
    python devtools/gen_docs.py --check    # 与磁盘比对，不一致则非零退出（门禁用）
"""

from __future__ import annotations

import asyncio
import inspect
import io
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

import gamemod_mcp.server as S                            # noqa: E402

ROOT = samples.ROOT
README = ROOT / "README.md"
SERVER = ROOT / "src" / "gamemod_mcp" / "server.py"

BEGIN = "<!-- BEGIN GENERATED: tools -->"
END = "<!-- END GENERATED: tools -->"

#: 节标题里这些字样不当作「工具层」（资源节没有工具）
SKIP_LAYER_KEYWORDS = ("资源",)


def parse_layers() -> list[tuple[int, str, int]]:
    """从 server.py 解析 `# N. 层名` 节，返回 [(序号, 层名, 行号)]。"""
    lines = SERVER.read_text(encoding="utf-8").splitlines()
    out = []
    pat = re.compile(r"^#\s*(\d+)\.\s*(.+?)\s*$")
    for i, ln in enumerate(lines):
        m = pat.match(ln)
        if m and not ln.startswith(" "):
            out.append((int(m.group(1)), m.group(2), i))
    return out


def parse_tool_layers() -> dict[str, int]:
    """工具名 → 层序号（按源码里的节位置推断，不手工维护映射）。"""
    lines = SERVER.read_text(encoding="utf-8").splitlines()
    layers = parse_layers()
    result: dict[str, int] = {}
    for i, ln in enumerate(lines):
        if not ln.lstrip().startswith("@mcp.tool("):
            continue
        for j in range(i + 1, min(i + 40, len(lines))):
            s = lines[j].lstrip()
            if s.startswith("@") or not s:
                continue
            m = re.match(r"def\s+([A-Za-z_]\w*)\s*\(", s)
            if m:
                name = m.group(1)
                num = layers[0][0] if layers else 0
                for lnum, _title, lidx in layers:
                    if lidx < i:
                        num = lnum
                result[name] = num
                break
    return result


def first_paragraph(doc: str) -> str:
    """docstring 首段（到第一个空行为止），压成一行。

    这是表格单元格的**唯一来源**。所以每个工具的 docstring 首段要么
    自己讲清楚，要么就不该在表格里出现更多信息。
    """
    if not doc:
        return ""
    out: list[str] = []
    for line in doc.strip().split("\n"):
        if not line.strip():
            break
        out.append(line.strip())
    return " ".join(out)


async def collect() -> tuple[list[dict], list[tuple[int, str]]]:
    tools = await S.mcp.list_tools()
    layers = [(n, t) for n, t, _ in parse_layers()
              if not any(k in t for k in SKIP_LAYER_KEYWORDS)]
    mapping = parse_tool_layers()
    # 层名做一次精简：去掉「（...）」括注，表格标题更干净
    def short(title: str) -> str:
        return re.sub(r"[（(].*?[)）]", "", title).strip()

    items = []
    for t in sorted(tools, key=lambda x: x.name):
        fn = S.RAW_TOOLS.get(t.name)
        doc = inspect.getdoc(fn) if fn else (t.description or "")
        items.append({
            "name": t.name,
            "layer": mapping.get(t.name, 0),
            "layer_label": short(dict((n, tt) for n, tt in
                                      [(x[0], x[1]) for x in parse_layers()])
                                 .get(mapping.get(t.name, 0), "其它")),
            "summary": first_paragraph(doc or ""),
        })
    return items, layers


def render(items: list[dict]) -> str:
    by_layer: dict[int, list[dict]] = {}
    for it in items:
        by_layer.setdefault(it["layer"], []).append(it)

    out: list[str] = []
    n = len(items)
    out.append(f"## 工具一览（{n} 个）")
    out.append("")
    out.append(f"> **本清单由 `python devtools/gen_docs.py` 从代码生成**"
               f"（来源：每个工具函数的 docstring 首段 + `server.py` 的层节注释）。")
    out.append("> **不要手工编辑这一节** —— 改了会在下次 `--check` 时报红。")
    out.append("> 要改描述，请改对应工具的 docstring 首段，然后跑 "
               "`python devtools/gen_docs.py --write`。")
    out.append(">")
    out.append("> 为什么改成生成：校验（`verify_tool_inventory.py`）只能**发现**漂移，")
    out.append("> 而生成本**不会产生**漂移 —— 本表的工具数曾手工漂过 5 次以上。")
    out.append("> 生成前做过「知识上移」核对：README 原有单元格里有 "
               "**402 个特征词**是 docstring 里没有的，")
    out.append("> 直接生成会丢掉那些手写事实，所以先把它们补进了 docstring 首段。")
    out.append("")
    for lnum, ltitle in sorted(by_layer):
        pass
    return "\n".join(out)


async def build_block() -> str:
    items, layers = await collect()
    layer_titles = {}
    for num, title in layers:
        layer_titles[num] = re.sub(r"[（(].*?[)）]", "", title).strip()

    by_layer: dict[int, list[dict]] = {}
    for it in items:
        by_layer.setdefault(it["layer"], []).append(it)

    # 层级标题里补一句适用说明（这几条是稳定的结构性事实，不是清单）
    layer_note = {
        1: "",
        2: "",
        3: "（**不需要游戏在运行**）",
        4: "（大结果集翻页）",
        5: "",
        6: "（截图 · OCR · 日志 · 等待）",
        7: "",
        8: "",
    }

    out: list[str] = []
    out.append(BEGIN)
    out.append(f"## 工具一览（{len(items)} 个）")
    out.append("")
    out.append("> **本清单由 `python devtools/gen_docs.py` 从代码生成**"
               "（来源：各工具函数的 docstring 首段 + `server.py` 的层节注释）。")
    out.append("> **不要手工编辑这一节** —— 下次 `--check` 会报红。")
    out.append("> 要改描述就改该工具的 docstring 首段，再跑 "
               "`python devtools/gen_docs.py --write`。")
    out.append(">")
    out.append("> 为什么改成生成：校验只能**发现**漂移，生成本**不会产生**漂移 "
               "—— 本表的工具数曾手工漂过 5 次以上。")
    out.append("> 改造前做过「知识上移」核对：原单元格里有 **402 个特征词**是 "
               "docstring 没有的，")
    out.append("> 直接生成会丢掉那些手写事实，所以先把它们补进了 docstring 首段。")
    out.append("")
    for num in sorted(by_layer):
        title = layer_titles.get(num, f"第 {num} 层")
        out.append(f"### {title}{layer_note.get(num, '')}")
        out.append("| 工具 | 作用 |")
        out.append("|---|---|")
        for it in by_layer[num]:
            out.append(f"| `{it['name']}` | {it['summary']} |")
        out.append("")
    out.append(END)
    return "\n".join(out)


def replace_block(readme_text: str, block: str) -> str:
    i = readme_text.find(BEGIN)
    j = readme_text.find(END)
    if i < 0 or j < 0:
        raise SystemExit(
            f"README 里找不到生成区块标记。请先手工把「## 工具一览」整段替换为：\n"
            f"{BEGIN}\n{END}")
    return readme_text[:i] + block + readme_text[j + len(END):]


def sync_inline_counts(text: str, tools: int, scripts: int,
                       suite: int) -> tuple[str, list]:
    """把散落在 README **正文**里的「N 个工具 / N 个脚本」数字改成实际值。

    为什么生成区块之外还要管这个：实测过 —— 区块生成好之后，
    正文里仍有 5 处硬编码的数字（第 5 行的「44 个工具覆盖」、
    目录树的「MCP 服务器（44 个工具）」、验证脚本表头的
    「44 个工具的语义标注」、报告段的「游戏 × 44 个工具」、
    以及「48 个可复现的验证脚本」）。
    它们与区块**属于同一类漂移**，只是位置不同。

    只替换**数字**，不动措辞 —— 所以这些句子仍可以人工调整，
    下次生成会把数字再对齐一次。
    """
    changes = []

    def sub(pat: str, want: int, label: str, s: str) -> str:
        m = re.search(pat, s)
        if not m:
            changes.append((label, "", "未匹配到（措辞可能变了）"))
            return s
        old = m.group(0)
        new = re.sub(r"\d+", str(want), old, count=1)
        if old != new:
            changes.append((label, old, new))
        return s.replace(old, new, 1)

    text = sub(r"\d+ 个工具覆盖", tools, "首段「N 个工具覆盖」", text)
    text = sub(r"MCP 服务器（\d+ 个工具）", tools, "目录树「MCP 服务器（N 个工具）」", text)
    text = sub(r"\d+ 个工具的\*\*语义标注\*\*", tools,
               "验证脚本表「N 个工具的语义标注」", text)
    text = sub(r"「游戏 × \d+ 个工具」", tools, "报告段「游戏 × N 个工具」", text)
    text = sub(r"\d+ 个可复现的验证脚本", scripts,
               "「N 个可复现的验证脚本」", text)
    # 紧跟其后的「其中 N 个进自动套件」也要同步 —— 它曾长期写着 43，
    # 而实测套件跑的是 50 个（`run_all_verify.py` 只排除
    # `verify_tool_coverage.py` 这个静态导航图）。数字分开写就会分开漂移。
    text = sub(r"其中 \d+ 个进自动套件", suite, "「其中 N 个进自动套件」", text)
    return text, changes


def count_verify_scripts() -> int:
    d = ROOT / "devtools"
    return len([p for p in d.glob("verify_*.py") if p.is_file()])


#: `run_all_verify.py` 明确排除、不进门禁的脚本（静态导航图）。
NOT_A_GATE = {"verify_tool_coverage.py"}


def count_suite_scripts() -> int:
    """真正进自动套件的脚本数（= 磁盘上的 verify_*.py 减去非门禁的）。"""
    d = ROOT / "devtools"
    return len([p for p in d.glob("verify_*.py")
                if p.is_file() and p.name not in NOT_A_GATE])


def main() -> int:
    write = "--write" in sys.argv
    check = "--check" in sys.argv
    block = asyncio.run(build_block())
    text = README.read_text(encoding="utf-8")
    try:
        new = replace_block(text, block)
    except SystemExit as exc:
        print(exc)
        return 2

    # 顺带同步正文里散落的数字（与区块同一类漂移）
    n_tools = block.count("| `")
    n_scripts = count_verify_scripts()
    n_suite = count_suite_scripts()
    new, changes = sync_inline_counts(new, n_tools, n_scripts, n_suite)

    if write:
        README.write_text(new, encoding="utf-8", newline="")
        print(f"已写回 README（工具表 {n_tools} 行，验证脚本 {n_scripts} 个，"
              f"其中进套件 {n_suite} 个）")
        for label, old, newv in changes:
            if old and old != newv:
                print(f"  同步正文数字：{label}: {old!r} -> {newv!r}")
            elif not old:
                print(f"  ⚠️ {label}")
        return 0
    if check:
        if new == text:
            print("README 与代码一致（工具表 + 正文数字，逐字节相同）")
            return 0
        print("❌ README 已漂移：重新生成的结果与磁盘不一致。")
        print("   修法：python devtools/gen_docs.py --write")
        i = text.find(BEGIN)
        j = text.find(END)
        old_block = text[i:j + len(END)] if i >= 0 and j >= 0 else ""
        for a, b in zip(old_block.split("\n"), block.split("\n")):
            if a != b:
                print(f"   [工具表] 磁盘: {a[:100]}")
                print(f"   [工具表] 生成: {b[:100]}")
                break
        for label, old, newv in changes:
            if old and old != newv:
                print(f"   [正文数字] {label}: 磁盘 {old!r} -> 应为 {newv!r}")
        return 1
    print(block)
    return 0


if __name__ == "__main__":
    sys.exit(main())
