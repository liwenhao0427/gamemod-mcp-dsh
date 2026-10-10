#!/usr/bin/env python3
"""校验「工具清单」在代码与文档之间是否一致。

为什么必须有这个脚本
============================================================
「有哪些工具」这件事在本项目里有 9 个地方被描述或复制：

    1. src/gamemod_mcp/server.py   —— 代码本身（唯一真值）
    2. src/gamemod_mcp/server.py   —— 模块 docstring 的「工具分层」列表
    3. src/gamemod_mcp/server.py   —— FastMCP(instructions=...) 里的工作流
    4. README.md                   —— 「工具一览」表格
    5. README.md                   —— 正文里的数量声明（「N 个工具」）
    6. skills/gamemod-mod/SKILL.md —— 工作流里引用的工具名
    7. 任何 *.md                   —— 提到已删 / 已合并的工具名时必须**同行标注**
    8. README.md                   —— 「验证脚本」表 vs devtools/verify_*.py
    9. ~/.dsh/skills/              —— 旧机制的 skill 副本 vs 仓库内（非 DSH 客户端读的那份）

其中 7、8、9 不属于「工具」本身，但属于同一类问题：**手工维护的副本必然漂移**。

经验证明靠人肉同步**必然漂移**（真实发生过的六起）：
    · README 写了并不存在的 `unhook_all`
    · server.py 的 docstring 漏写了 13 个工具
    · README 的「41 个工具」与实际数量不符
    · README 写「10 个可复现的验证脚本」，而实际已有 20 多个，
      同一节的表格还漏了 9 个脚本 —— 当时**没有任何检查**会发现
    · docs/ 里写着「全部工具清单（41 个）」（实际 38），§三 的工作流还让
      Agent 去调已删除的 `detect_games()` —— 同样**没有任何检查**覆盖 docs/
    · 已安装的 skill 副本落后仓库 139 行、开头还在教已删除的工具
      —— 也就是说「仓库修好了、门禁全绿」与「Agent 照着删掉的工具去调」
      可以**同时成立**（修好的那份不是被读的那份）

所以用本脚本卡住：**不一致就非零退出**。

校验规则
============================================================
代码（@mcp.tool 装饰的函数）是唯一真值，其余都是「必须与之一致」的副本。

- README 表格：必须是**双向完全一致**（不多、不少）。
  这是权威清单，Agent 与人都靠它了解全貌。
- README 正文里的数量：「N 个工具」这类数字必须等于实际数量。
- server.py docstring 分层列表：双向一致（它声明了每个工具的归属层）。
- instructions：**只许提到存在的工具**（它是精简工作流，不必列全）。
- SKILL.md：**只许提到存在的工具**（它是使用指南，不必列全），
  但要能识别出「曾经存在、现已删除」的名字。
- 任何 *.md 提到已删工具：**同一行**必须能看出它已经没了
  （历史记录有价值，但 Agent 会照着它去调）。
- README 的「验证脚本」表：与 `devtools/verify_*.py` 双向一致。
- 已安装的 skill 副本：与仓库内**逐字节相同**；副本不存在时记为
  「无法验证」并跳过（见 AGENTS.md §5.3），但要说出来。

用法
============================================================
    python devtools/verify_tool_inventory.py          # 校验，返回 0/1
    python devtools/verify_tool_inventory.py --list    # 顺便打印清单
"""

from __future__ import annotations

import io
import os as _os
import pathlib
import re
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
_sys.stdout = io.TextIOWrapper(_sys.stdout.buffer, encoding="utf-8",
                               errors="replace")

import samples  # noqa: E402

ROOT = samples.ROOT
SERVER = ROOT / "src" / "gamemod_mcp" / "server.py"
README = ROOT / "README.md"
SKILL = ROOT / "skills" / "gamemod-mod" / "SKILL.md"

# SKILL.md 里被反引号包起来的「字段名/参数名」，不是工具名。
# 必须显式列出，否则「工具名」与「参数名」在文本上无法区分。
NON_TOOL_IDENTIFIERS = {
    # 通用参数 / 返回字段
    "args_type", "retval_type", "max_calls", "backtrace", "hook_id",
    "preferred_module", "auto_find", "name_filter", "only_games",
    "include_inherited", "base_full_name", "full_name", "method_name",
    "metadata_path", "cache_dir", "force_rebuild", "game_dir",
    "nested_name", "param_count", "parameter_count", "return_type",
    "type_confidence", "field_count", "method_count", "type_count",
    "install_dir", "appid", "output_path", "include_entries",
    "exclude_compiler_generated", "subtype_of", "only_enums",
    "output_dir", "split_files", "assembly", "type_name", "source",
    "has_more", "cache_hit", "readOnlyHint", "destructiveHint",
    # P2-4a 起句柄失败时会返回它（可用句柄清单），是返回字段不是工具
    "available_handles",
    # launch_game 被同名进程拦下时返回「是哪个 pid 在跑」，是返回字段不是工具
    "running_pid",
    # set_field_value 的返回字段：写完回读到的实际值（`verified` 的依据）
    "read_back", "write_width", "write_kind", "value_before",
    # find_instances 的参数与返回字段
    "max_count", "overflow", "unsupported",
    "exact_class_count", "derived_count", "class_name_counts",
    # find_instances 返回的每个实例带的字段（真实类名）
    "class_name", "click_point", "coordinate_space", "best_match",
    # wait_for 的参数（与 field/instances 分支共用）
    "min_count", "equals", "timeout_ms", "interval_ms", "verdict",
    # verify_state 的参数与返回字段
    "require_independent", "checks", "independent_count", "verdict_reason",
    # SKILL.md 配方 8（模拟按键）里提到的 Win32 常量与「本项目**没有**的
    # 工具名」：`send_key` 是明说「本工具集没有它、所以走 eval_script」
    # 时提到的，不是引用一个存在的工具。INPUT_KEYBOARD 是 Win32 常量。
    "INPUT_KEYBOARD", "send_key", "MOUSEEVENTF_ABSOLUTE",
    # 「外面套了一层」时返回的建议路径（游戏根），是返回字段不是工具
    "game_root",
    "image_count", "rva_hex", "hex_data", "module", "offset",
    # Mono 运行时探针用到的 Mono C API 导出名（不是 MCP 工具，是探针内部调的 C 函数）
    "mono_compile_method", "mono_gc_disable", "mono_gc_enable",
    # IL2CPP 运行时探针用到的 IL2CPP C API 导出名（同上，不是 MCP 工具）。
    # `il2cpp_thread_attach/detach` 是「收尾后游戏崩溃」那节的根因说明：
    # 探针把 Frida 线程挂进域却没摘，卸载后游戏在 GC 时崩。属引擎 API 名。
    "il2cpp_thread_attach", "il2cpp_thread_detach",
    # set_field_value 文档里说明「静态字段该用哪个引擎 API」时提到它，
    # 是 IL2CPP C API 导出名，不是 MCP 工具。
    "il2cpp_field_static_set_value",
    # SKILL.md「语义化点击 UI」配方里演示 eval_script 时用到的 IL2CPP
    # C API 导出名（引擎 API，不是 MCP 工具）
    "il2cpp_class_get_method_from_name", "il2cpp_object_get_class",
    "il2cpp_runtime_invoke",
    # close_session 返回的 steps 字段（表明探针线程已从引擎域摘除），
    # 是返回字段不是工具
    "domain_released",
    # close_session 在探针卡死时返回的两个字段：
    # `wedged` = 探针已卡死；`session_released` = 会话对象已作废（可以重新注入），
    # 与 `restored`（游戏是否已恢复原状）是**两件事**。都是返回字段不是工具。
    "wedged", "session_released",
    # 类名没解析出来时返回的「已搜过几个程序集」，是返回字段不是工具
    "searched_images",
    # decompile 返回的「实际反编译出来的类型名」（嵌套类型会退让到最外层，
    # 所以它与 requested 不一定相同），是返回字段不是工具
    "decompiled_type",
    # launch_game / inject_process 跟随目标换了游戏时返回的说明字段，
    # 以及反推不出游戏目录时的提示字段 —— 都是返回字段不是工具
    "context_switched", "context_warning",
    # drain_events 返回的「队列里还剩多少条」（近似值），是返回字段不是工具。
    # 它不是可有可无的装饰：`kind` 过滤返回空时，调用方要靠它区分
    # 「真的没有该类型事件」与「队列里还有东西」。
    "pending_approx",
    # 探针主动上报的事件类型（`send()` 的非 Hook 消息）。
    # `probe_ready` 是探针就绪通知；`script_error` 是探针脚本抛错时的事件类型。
    # 两者都是 `drain_events(kind=...)` 的取值，不是 MCP 工具。
    "probe_ready", "script_error",
    # 「工具调用卡住」那节讲的是**实现机制**，提到的两个名字是内部函数/API，
    # 不是 MCP 工具：`wait_ready` 是 RuntimeSession 的轮询入口、
    # `to_thread` 是 asyncio 的标准库函数。写出来是为了让读文档的人
    # 能对上代码，Agent 并不会去调它们。
    "wait_ready", "to_thread",
    # 非本项目的标识符（引擎/框架 API）
    "get_frameCount", "MonoBehaviour", "Il2CppClass", "GameAssembly",
}

# 曾经存在、现已删除的工具名。看到它们出现在文档里 = 文档没跟上代码。
REMOVED_TOOLS = {
    "detect_games":       "移除：全机扫描属噪声工具，运行上下文限定在游戏项目目录",
    "find_monobehaviours": "合并进 find_derived_classes(assembly=...)",
    "get_method_source":  "合并进 class_detail(method_name=...)",
    "unhook_all":         "从未存在过（README 的历史笔误），卸载全部用 unhook()",
    "search_query_result": "合并进 read_query_result(pattern=...)（前者是后者在 pattern='' 时的特例）",
}

# 分层列表里的「层名」等非工具词，解析时需要排除。
LAYER_WORDS = {
    "UE", "IL2CPP", "Mono", "MCP", "API",
}

PASS, FAIL = [], []


def check(label: str, cond: bool, detail: str = "") -> bool:
    tag = "✅" if cond else "❌"
    print(f"  {tag} {label}" + (f" — {detail}" if detail else ""))
    (PASS if cond else FAIL).append(label)
    return cond


def show(title: str) -> None:
    print()
    print("=" * 76)
    print(title)
    print("=" * 76)


def code_tools() -> set[str]:
    """从 server.py 抽出所有 @mcp.tool 装饰的函数名 —— 唯一真值。

    做法是**逐行看「装饰器 + 紧随的 def」**，而不是用正则去配对括号：

    P2-5 把标注从字面量 dict 换成 `_ann(...)` 调用后，旧正则
    `@mcp\\.tool\\([^)]*\\)\\s*\\ndef` 里的 `[^)]*` 会在 `_ann(...)` 的
    **第一个** `)` 上停下，接着要求 `\\ndef` 而实际是第二个 `)`，
    于是**一个工具都抽不出来**。当时靠本文件的「解析出 0 个工具 = 解析规则
    失效」这条自检才发现 —— 否则会退化成「两边都是空集，所以完全一致」。

    P2-4 又踩了同一个坑的另一种形态：响应上限是**第二个装饰器**
    （`@_cap_response`），而它既不是空行也不是注释，于是这里 `break` 掉，
    又变成「一个工具都抽不出来」。所以规则改成**跳过所有装饰器行**，
    找第一个真正的 `def` —— 装饰器以后还会增加，这条规则不该再改。
    """
    lines = SERVER.read_text(encoding="utf-8").splitlines()
    out: set[str] = set()
    for i, line in enumerate(lines):
        if not line.lstrip().startswith("@mcp.tool("):
            continue
        # 先把这个装饰器自身的括号配平（`_ann(...)` 可能跨多行），
        # 否则第 3 行那种续行会被当成「不是装饰器也不是 def」而 break 掉，
        # 导致该工具**被静默漏掉** —— 而漏掉的后果是「门禁全绿但少算一个工具」，
        # 比报红更糟（实测：set_field_value 用多行装饰器时门禁仍报 38/38）。
        depth = line.count("(") - line.count(")")
        j = i
        while depth > 0 and j + 1 < len(lines):
            j += 1
            depth += lines[j].count("(") - lines[j].count(")")
        for nxt in lines[j + 1:]:
            s = nxt.strip()
            # 空行 / 注释 / **还有别的装饰器** 都继续往下找
            if not s or s.startswith("#") or s.startswith("@"):
                continue
            m = re.match(r"def\s+([A-Za-z_]\w*)\s*\(", s)
            if m:
                out.add(m.group(1))
            break
    return out


def backticked(text: str) -> set[str]:
    return set(re.findall(r"`([A-Za-z_][A-Za-z0-9_]*)`", text))


def called_names(text: str) -> set[str]:
    """抽出「像工具调用」的标识符，含代码块里的 `name(` 形式。

    只扫反引号是不够的 —— SKILL.md 的配方大多写在 ```python 代码块里，
    形如 `find_monobehaviours(name_pattern="Manager")`，
    **不带反引号**。曾经正是这种形式让一个已删除的工具名留在了文档里
    而校验脚本毫无察觉（`find_monobehaviours` 就是被漏掉的那个）。
    """
    return set(re.findall(r"\b([a-z][a-z0-9_]*)\s*\(", text))


def readme_table_tools() -> set[str]:
    """README「工具一览」表格第一列里的工具名。

    该表格有 `| \\`unhook\\` / \\`hook_status\\` | ... |` 这种一格多工具的写法，
    所以要把一格里的所有反引号标识符都取出来。
    """
    text = README.read_text(encoding="utf-8")
    m = re.search(r"^##\s*工具一览.*?$(.*?)(?=^##\s|\Z)",
                  text, re.S | re.M)
    if not m:
        return set()
    names: set[str] = set()
    for line in m.group(1).splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = line.split("|")
        if len(cells) < 2:
            continue
        first = cells[1]
        for tok in re.findall(r"`([A-Za-z_][A-Za-z0-9_]*)`", first):
            names.add(tok)
    return names


def server_docstring_tools() -> set[str]:
    """server.py 模块 docstring 里「工具分层」列表提到的工具名。

    注意这份列表**不用反引号**，且**会折行**，形如：
        1. 发现层      : find_game_processes / inspect_game / inspect_binary
        3. 静态查询层  : search_classes / class_detail /
                         search_methods / class_hierarchy /
    所以不能只取「冒号后那一行」——续行（没有 `N. 层名 :` 前缀）同样要收，
    否则会把折行写到第二行的工具全部误判成「docstring 漏收」。
    """
    src = SERVER.read_text(encoding="utf-8")
    doc = src.split('"""', 2)[1]
    m = re.search(r"工具分层(.*?)关键约定", doc, re.S)
    if not m:
        return set()
    names: set[str] = set()
    for line in m.group(1).splitlines():
        # 首行形如「    1. 发现层      : find_game_processes / ...」
        # 续行没有这个前缀，整行都是工具名。
        head = re.match(r"\s*\d+\.\s*\S+\s*:\s*(.*)$", line)
        part = head.group(1) if head else line
        for tok in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", part):
            names.add(tok)
    return names


def main() -> int:
    argv = set(_sys.argv[1:])
    tools = code_tools()

    print("工具清单一致性校验")
    print(f"  真值来源: {SERVER.relative_to(ROOT)}")
    print(f"  当前工具数: {len(tools)}")

    if not tools:
        print("\n❌ 没能从 server.py 解析出任何工具 —— 解析规则失效了，"
              "请先修正本脚本再谈一致性。")
        return 1

    # ------------------------------------------------------------ 1. README 表格
    show("1. README「工具一览」表格 vs 代码（必须双向完全一致）")
    rm = readme_table_tools()
    print(f"  README 表格列出 {len(rm)} 个")

    only_code = sorted(tools - rm)
    only_readme = sorted(rm - tools)

    check("代码里有、README 漏掉的工具为 0", not only_code,
          f"{only_code}" if only_code else "（无遗漏）")
    check("README 写了、代码里没有的工具为 0", not only_readme,
          f"{only_readme}" if only_readme else "（无多余）")

    # 历史笔误单独点名，便于快速定位
    stale = sorted(rm & set(REMOVED_TOOLS))
    if stale:
        for s in stale:
            print(f"      ⚠️  `{s}`：{REMOVED_TOOLS[s]}")

    # ------------------------------------------------------------ 2. README 数字
    show("2. README 里声明的工具数量 vs 实际")
    rtext = README.read_text(encoding="utf-8")
    # 抓「41 个工具」这类数字（含「工具一览（41 个）」）
    nums = set()
    for pat in (r"(\d+)\s*个工具", r"工具一览（(\d+)\s*个）",
                r"工具一览\((\d+)\s*个\)"):
        nums |= {int(x) for x in re.findall(pat, rtext)}
    for n in sorted(nums):
        check(f"README 写的 {n} 与实际 {len(tools)} 一致", n == len(tools),
              "" if n == len(tools) else f"实际是 {len(tools)}")
    if not nums:
        check("README 里找到了工具数量声明", False,
              "没找到「N 个工具」字样（工具清单应说明规模）")

    # ------------------------------------------------- 2b. 生成式一致性
    #
    # 第 1 节是「表格里的工具名 vs 代码」—— 它能发现**名字**漂移。
    # 但描述文字、层级归属、表格结构漂了它是看不出来的。
    # 所以再加一条更强的：README 那一节必须**逐字节等于重新生成的结果**。
    #
    # 为什么这条比第 1 节更强：第 1 节是「校验」，只能发现漂移；
    # 生成则**不会产生**漂移 —— 本表的工具数曾手工漂过 5 次以上。
    show("2b. README「工具一览」必须等于 gen_docs.py 的生成结果（生成 > 校验）")
    import subprocess
    gen = ROOT / "devtools" / "gen_docs.py"
    if not gen.is_file():
        check("gen_docs.py 存在", False, "生成脚本不见了 —— 门禁无法执行")
    else:
        r = subprocess.run([_sys.executable, str(gen), "--check"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=180)
        out = (r.stdout or "") + (r.stderr or "")
        check("README 工具表与代码生成结果一致", r.returncode == 0,
              out.strip().splitlines()[0] if out.strip() else f"exit={r.returncode}")
        # 非空转：确认 README 里真的有生成区块标记
        rtext2 = README.read_text(encoding="utf-8")
        check("README 里有生成区块标记（否则上面那条是空转的）",
              "<!-- BEGIN GENERATED: tools -->" in rtext2
              and "<!-- END GENERATED: tools -->" in rtext2,
              "缺标记则 replace_block 会直接报错，这里提前拦住")

    # ------------------------------------------------------------ 3. server docstring
    show("3. server.py 模块 docstring「工具分层」列表 vs 代码")
    dt = server_docstring_tools()
    print(f"  docstring 列出 {len(dt)} 个")
    miss_doc = sorted(tools - dt)
    extra_doc = sorted(dt - set(NON_TOOL_IDENTIFIERS) - LAYER_WORDS - tools)
    check("代码里的工具都被 docstring 收录", not miss_doc,
          f"{miss_doc}" if miss_doc else "（无遗漏）")
    check("docstring 没有不存在的工具", not extra_doc,
          f"{extra_doc}" if extra_doc else "（无多余）")

    # ------------------------------------------------------------ 4. instructions
    show("4. FastMCP(instructions=...) 只许提到存在的工具")
    doc = SERVER.read_text(encoding="utf-8").split('"""', 2)[1]
    src = SERVER.read_text(encoding="utf-8")
    m = re.search(r"instructions=\((.*?)\n    \),", src, re.S)
    inst = m.group(1) if m else ""
    inst_tools = set(re.findall(r"(\w+)\(", inst))
    # 括号里的调用形式才是工具调用；排掉 xx()  这类空壳
    inst_tools = {t for t in inst_tools if t in tools or t in REMOVED_TOOLS}
    ghost = sorted(inst_tools & set(REMOVED_TOOLS))
    check("instructions 里没有已删除的工具", not ghost,
          f"{ghost}" if ghost else "（干净）")
    check("instructions 提到了工具", bool(inst_tools),
          f"{len(inst_tools)} 个：{sorted(inst_tools)}")

    # ------------------------------------------------------------ 5. SKILL.md
    show("5. SKILL.md 只许提到存在的工具")
    stext = SKILL.read_text(encoding="utf-8")
    stoks = backticked(stext)
    # 反引号里的 snake_case 标识符：排除已知字段名与非工具名
    suspicious = sorted(
        t for t in stoks
        if "_" in t
        and t not in tools
        and t not in NON_TOOL_IDENTIFIERS
    )
    check("SKILL.md 没有引用不存在的工具名", not suspicious,
          f"{suspicious}" if suspicious else "（干净）")

    ghost_skill = sorted(stoks & set(REMOVED_TOOLS))
    check("SKILL.md 没有引用已删除的工具", not ghost_skill,
          f"{ghost_skill}" if ghost_skill else "（干净）")

    # 代码块里的调用形式（不带反引号）也必须扫 —— 这是最容易漏的一类
    skill_calls = called_names(stext)
    ghost_calls = sorted(skill_calls & set(REMOVED_TOOLS))
    check("SKILL.md 代码块里没有已删除工具的调用", not ghost_calls,
          f"{ghost_calls}" if ghost_calls else "（干净）")

    # 代码块里调用的、既不是工具也不是已知非工具函数的名字 —— 提示可能写错了
    unknown_calls = sorted(
        n for n in skill_calls
        if n not in tools and n not in NON_TOOL_IDENTIFIERS
    )
    # 只报告「像工具名」的（含下划线，排除 print/sorted/len 这类内建）
    toolish = [n for n in unknown_calls if "_" in n]
    check("SKILL.md 没有调用不存在的工具类函数", not toolish,
          f"{toolish}" if toolish else "（干净）")

    # SKILL.md 是使用指南，不要求列全；但至少要覆盖核心工作流
    core = {"open_game", "search_classes", "class_detail", "hook_method"}
    check("SKILL.md 覆盖了核心工作流工具", core <= stoks,
          f"缺少 {sorted(core - stoks)}" if not (core <= stoks) else "（齐全）")

    # ------------------------------------------------------------ 6. 已删除工具
    show("6. 已删除工具不得在任何文档中残留")
    for name, why in sorted(REMOVED_TOOLS.items()):
        spots = []
        if name in rm:
            spots.append("README 工具表")
        if name in stoks:
            spots.append("SKILL.md")
        if name in dt:
            spots.append("server docstring")
        if name in inst_tools:
            spots.append("instructions")
        # 代码里当然也不该有
        if re.search(rf"\ndef\s+{name}\(", src):
            spots.append("代码")
        check(f"`{name}` 已清理", not spots,
              f"仍出现在 {spots}（{why}）" if spots else f"（{why}）")

    # --------------------------------------------------- 7. README 验证脚本表
    show("7. README「验证脚本」表 vs devtools/（必须双向完全一致）")
    # 本节与工具无关，但和「工具清单」是同一类问题：**手工维护的清单会漂移**。
    # README 曾写「devtools 下有 10 个可复现的验证脚本」而实际已有 20 多个，
    # 同节表格还漏了 9 个脚本 —— 而且没有任何检查会发现。
    on_disk = {p.name for p in (ROOT / "devtools").glob("verify_*.py")}
    in_readme = set(re.findall(r"`(verify_[A-Za-z0-9_]+\.py)`", rtext))
    print(f"  devtools 实际 {len(on_disk)} 个，README 表格列出 {len(in_readme)} 个")

    miss_scripts = sorted(on_disk - in_readme)
    extra_scripts = sorted(in_readme - on_disk)
    check("devtools 里的验收脚本都被 README 收录", not miss_scripts,
          f"{miss_scripts}" if miss_scripts else "（无遗漏）")
    check("README 写了、devtools 里没有的脚本为 0", not extra_scripts,
          f"{extra_scripts}" if extra_scripts else "（无多余）")

    script_nums = {int(x) for x in
                   re.findall(r"(\d+)\s*个可复现的验证脚本", rtext)}
    for n in sorted(script_nums):
        check(f"README 写的「{n} 个可复现的验证脚本」与实际 {len(on_disk)} 一致",
              n == len(on_disk), "" if n == len(on_disk) else f"实际是 {len(on_disk)}")
    if not script_nums:
        check("README 声明了验证脚本总数", False,
              "没找到「N 个可复现的验证脚本」字样")

    # --------------------------------------------------- 8. 已删工具的文档残留
    show("8. 已删除 / 已合并的工具：出现在文档里必须**标注**")
    # 前 7 节里与「工具名出现在哪」有关的，只有四处：
    # README 工具表 / server docstring / instructions / SKILL.md。
    # **其它文档不在任何检查范围内** —— `docs/项目梳理与优化建议.md`
    # 就长期写着「全部工具清单（41 个）」（实际 38），§三 的工作流还让
    # Agent 去调已删除的 `detect_games()`。
    # 一个已删除的工具名出现在**指导性**段落里，Agent 会照着去调，
    # 而它比「README 表格写错」更难发现：没有检查覆盖 docs/。
    # 所以这里补一条通用规则：提到它们可以（历史记录有价值），
    # 但同一行必须能看出「它已经没了」。
    doc_files = [p for p in ROOT.rglob("*.md")
                 if "agent_logs" not in p.parts and ".git" not in p.parts
                 and "node_modules" not in p.parts]
    # 「已经没了」的标记词。故意放宽 —— 这一节的目的是**消除歧义**，
    # 不是逼人用某个固定句式。
    GONE = re.compile(r"已删|删掉|删除|移除|已移除|已合并|合并|~~|旧|曾经|更名|"
                      r"不再|当时|历史|不存在|从未")
    mentions = 0
    offenders: list[str] = []
    for p in sorted(doc_files):
        for lineno, line in enumerate(
                p.read_text(encoding="utf-8").splitlines(), 1):
            for name in REMOVED_TOOLS:
                if name not in line:
                    continue
                mentions += 1
                if not GONE.search(line):
                    offenders.append(
                        f"{p.relative_to(ROOT)}:{lineno} `{name}` —— "
                        f"{line.strip()[:56]}")
    print(f"  扫描 {len(doc_files)} 个 .md，命中已删工具名 {mentions} 处")
    check("扫到了文档文件（否则下面的断言是空转的）", len(doc_files) >= 8,
          f"{len(doc_files)} 个")
    # 非空转保证：这些名字**确实**还在文档里被提到（历史记录），
    # 所以「没有未标注的提及」不是因为扫了个空集合。
    check("这些已删工具名在文档里确实被提到（证明扫描不是空转）",
          mentions >= 5, f"{mentions} 处")
    check("提到已删工具的地方都做了标注（没有让 Agent 以为它还在）",
          not offenders,
          (f"{len(offenders)} 处未标注（**把「已删/已合并」写到同一行**即可）："
           + "；".join(offenders[:4])) if offenders else "（全部有标注）")

    # ------------------------------------------------- 9. 已安装的 skill 副本
    show("9. 已安装的 skill 副本（旧机制）若存在，必须与仓库内一致")
    # ⚠️ 这一节守的是**旧机制**，现在已被组合包取代，但仍然保留，原因见下。
    #
    # 旧机制：手工把 `skills/` 复制到 `~/.dsh/skills/`，Agent 读的是那份**副本**。
    # 那份副本不在任何门禁范围内 —— 实测曾落后 **139 行**（375 vs 514），
    # 开头第一句还写着「动手前第一步永远是 `detect_games()`」，而那工具早已删除。
    # 也就是说「仓库修好了、文档门禁全绿」与「Agent 照着删掉的工具去调」
    # 可以同时成立。
    #
    # 新机制：组合包在 `index.js` 里用 `ctx.inject(['skills'], …)` **直接注册**
    # 随包分发的那一份，Agent 看到的就是本版本包里的内容，不存在副本漂移。
    # 而且它注册为 runtime 候选（rank 250），**优先于** `~/.dsh/skills/`
    # 这个 user-dsh 根（rank 400，数值小的胜出）—— 所以残留的旧副本
    # 不会盖掉新内容。
    #
    # 那为什么还留着这一节：旧副本对**非 DSH 客户端**（Claude Code / Cursor 等）
    # 仍是实际生效的那一份，它一旦漂移，那些用户照样会照着一个已删除的工具去调。
    # 所以继续卡住它 —— 只是它不再是 DSH 侧的真源。
    #
    # 跳过规则遵循 AGENTS.md §5.3：副本不存在（没装过 / 别的机器）时记为
    # **「无法验证」并跳过** —— 但要说出来，不许假装通过。
    installed = pathlib.Path.home() / ".dsh" / "skills" / "gamemod-mod" / "SKILL.md"
    if not installed.exists():
        print(f"  ⚠️ {installed} 不存在 —— 无法验证，跳过（这不是通过）")
    else:
        src_bytes = SKILL.read_bytes()
        dst_bytes = installed.read_bytes()
        same = src_bytes == dst_bytes
        detail = ""
        if not same:
            import hashlib
            a = hashlib.sha256(src_bytes).hexdigest()[:16]
            b = hashlib.sha256(dst_bytes).hexdigest()[:16]
            detail = (f"仓库 {len(src_bytes)} 字节/{a} vs 已安装 "
                      f"{len(dst_bytes)} 字节/{b} —— 把仓库内的 "
                      f"skills/gamemod-mod/SKILL.md 重新复制过去")
        check("已安装的 gamemod-mod skill 副本与仓库内逐字节相同", same, detail)

    # ------------------------------------------------------------ 汇总
    if "--list" in argv:
        show("当前工具清单")
        for t in sorted(tools):
            print(f"    {t}")

    print()
    print("=" * 76)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print(f"  - {f}")
        print()
        print("→ 工具清单已漂移：请按 AGENTS.md §4「必须同步的五处」改完再提交。")
    print("=" * 76)
    return 1 if FAIL else 0


if __name__ == "__main__":
    _sys.exit(main())
