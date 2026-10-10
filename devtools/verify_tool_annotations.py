"""验收：38 个 MCP 工具的**语义标注**（P2-5）。

要证明的问题
--------------------------------------------------
`annotations` 有 4 个语义位，而它们的默认值**不是「都关」**：

    readOnlyHint  默认 False
    destructiveHint 默认 **True**     ← 不写 = 告诉客户端「它会破坏东西」
    idempotentHint  默认 False
    openWorldHint   默认 **True**     ← 不写 = 告诉客户端「它与外部世界交互」

所以在改这个脚本之前，`search_classes`（只读本地 SQLite）在客户端眼里
也是「破坏性 + 开放世界」—— 默认值是保守的，但用在本项目上全是错的。
现在**每个**工具都显式表态（`server.py` 的 `_ann()`）。

同时修掉两个**写错的**标注：

  · `drain_events` 曾标 `readOnlyHint=True`，但它是「**取走即消费**」——
    事件被消费掉就没了。标成只读会让客户端以为重试无害。
  · `decompile` / `ue_profile` 给了 `output_dir` / `output_path` 就会**落盘**，
    标成只读是不对的（它们只写自己的产物、可覆盖，所以不算破坏）。

这个脚本守什么
--------------------------------------------------
1. 五（四）个语义位**每位都有值** —— 不许把语义留给默认值。
2. 三个分组（只读本地 / 只读但要游戏进程 / 非只读）与代码**双向完全一致**；
   新增工具不在这张表里就直接报红 —— 逼着作者**当面表态**它危不危险。
3. 蕴含关系：只读 ⇒ 不破坏且无副作用（不许出现自相矛盾的标注）。
4. `_ann()` 自身会拦住矛盾组合（`read_only=True` + `destroy=True` 抛错）——
   否则这个「防矛盾」的设计只是个说法。
5. 点名那三个曾经写错/容易写错的工具，防止有人改回去。
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


#: 只读，且只读本地文件 / 本地 SQLite —— 既不碰游戏进程也不联网。
#: 这些是 `openWorldHint=False`。
READONLY_LOCAL = {
    "inspect_game", "inspect_binary", "get_session", "search_classes",
    "class_detail", "search_fields", "search_methods", "class_hierarchy",
    "find_derived_classes", "analyze_dependency", "list_images",
    "read_query_result", "detect_ue", "ue_list_entries", "ue_manifest",
    "ue_script_objects", "get_diagnostics",
    # probe_log 读**本地日志文件**：不碰游戏进程、不联网、也不消费文件
    "probe_log",
}

#: 只读，但要有一个**活着的**游戏进程才答得出来 → `openWorldHint=True`。
READONLY_OPEN = {
    "find_game_processes", "runtime_info", "runtime_class_info",
    "find_hook_target", "hook_status", "read_memory", "read_field_value",
    "scan_pattern", "find_instances",
}

#: 非只读。name -> (destructiveHint, idempotentHint, openWorldHint)
MUTATING: dict[str, tuple[bool, bool, bool]] = {
    # 改了东西，但可逆、也不破坏 —— 客户端不必按「危险」处理
    "open_game":      (False, True, False),   # 同一目录重复调会复用
    "close_session":  (False, True, False),   # 卸载 Hook 是**恢复**原状
    "decompile":      (False, True, False),   # 给 output_dir 才落盘，只写自己的产物
    "ue_profile":     (False, True, False),   # 同上（output_path）
    "inject_process": (False, True, True),    # 同一进程重复注入会复用
    "gc_control":     (False, True, True),    # 可开可关
    # 截图/OCR 会**落盘**（PNG），所以不是只读；但落的是自己的产物，
    # 重复调用等价（同一画面截两次内容一致），不破坏任何东西。
    "capture_screen": (False, True, True),
    "ocr":            (False, True, True),
    # wait_for 会**消费时间**并可能落盘（ocr 分支），但重复等待同一条件
    # 等价（条件成立就一直成立）；它不写内存、不装 Hook。
    "wait_for":       (False, True, True),
    # verify_state 与 wait_for 同源：只读求值多个条件，不写内存、不装 Hook；
    # 重复验收同一状态等价。
    "verify_state":   (False, True, True),
    # 每次调用都产生新的东西，重复调不等价
    "launch_game":    (False, False, True),   # 每次都是新进程
    "hook_method":    (False, False, True),
    "hook_address":   (False, False, True),
    # 破坏性：不可逆 / 可能崩游戏 / 数据被消费掉
    "unhook":         (True, True, True),     # 不带 id 会把 Hook 全卸了
    "drain_events":   (True, False, True),    # **取走即消费**，事件没了就没了
    "write_memory":   (True, True, True),     # 写错地址立刻崩游戏
    "set_field_value": (True, True, True),    # 同上：写错值/地址立刻崩游戏
    "eval_script":    (True, False, True),    # 游戏进程内任意代码执行
}

#: 必须声明为破坏性的工具（客户端要弹确认的就是它们）
MUST_BE_DESTRUCTIVE = {"write_memory", "set_field_value", "eval_script",
                       "unhook", "drain_events"}

#: **曾经写错**的标注 —— 点名钉住，防止有人改回去
MUST_NOT_BE_READONLY = {
    "drain_events": "取走即消费：事件被消费掉就没了，标只读会让客户端以为重试无害",
    "decompile": "给了 output_dir 会落盘（写自己的产物，不算破坏）",
    "ue_profile": "给了 output_path 会落盘（同上）",
}

BITS = ("title", "readOnlyHint", "destructiveHint",
        "idempotentHint", "openWorldHint")


def main() -> int:
    section("MCP 工具语义标注（P2-5）")

    tools = asyncio.run(S.mcp.list_tools())
    live = {t.name: t.annotations for t in tools}
    print(f"  实测工具数 {len(tools)}")

    # ---------------------------------------------- 1. 每位都有值
    print()
    print("  -- 1. 4 个语义位 + title 都必须显式有值（不许吃默认值）--")
    missing = [(n, b) for n, a in live.items() for b in BITS
               if a is None or getattr(a, b, None) is None]
    check("38 个工具都写全了标注", not missing,
          f"缺 {len(missing)} 处：{missing[:6]}" if missing else
          f"{len(tools)} 个工具 × {len(BITS)} 位")

    # ---------------------------------------------- 2. 分组与代码一致
    print()
    print("  -- 2. 「只读本地 / 只读要进程 / 非只读」与代码双向一致 --")
    expected = set(READONLY_LOCAL) | set(READONLY_OPEN) | set(MUTATING)
    not_in_table = sorted(set(live) - expected)
    not_in_code = sorted(expected - set(live))
    check("代码里的工具都在本表里表过态", not not_in_table,
          f"**新增工具必须在这里声明它危不危险**：{not_in_table}"
          if not_in_table else f"{len(expected)} 个")
    check("本表里没有已删除的工具", not not_in_code,
          f"表里多出：{not_in_code}" if not_in_code else "无多余")
    dup = (set(READONLY_LOCAL) & set(READONLY_OPEN)) | \
          (set(READONLY_LOCAL) & set(MUTATING)) | \
          (set(READONLY_OPEN) & set(MUTATING))
    check("三个分组互不重叠", not dup, f"重复：{sorted(dup)}" if dup else "无重叠")

    # ---------------------------------------------- 3. 逐项核对
    print()
    print("  -- 3. 逐项核对：只读组 --")
    bad_ro: list[str] = []
    for name, open_world in [(n, False) for n in sorted(READONLY_LOCAL)] + \
                            [(n, True) for n in sorted(READONLY_OPEN)]:
        a = live.get(name)
        if a is None:
            bad_ro.append(f"{name}(不存在)")
            continue
        if a.readOnlyHint is not True:
            bad_ro.append(f"{name}.readOnlyHint={a.readOnlyHint}")
        if a.destructiveHint is not False:
            bad_ro.append(f"{name}.destructiveHint={a.destructiveHint}")
        if a.idempotentHint is not True:
            bad_ro.append(f"{name}.idempotentHint={a.idempotentHint}")
        if a.openWorldHint is not open_world:
            bad_ro.append(f"{name}.openWorldHint={a.openWorldHint}"
                          f"(应为 {open_world})")
    check(f"{len(READONLY_LOCAL) + len(READONLY_OPEN)} 个只读工具标注正确",
          not bad_ro, f"不符 {len(bad_ro)} 项：{bad_ro[:5]}" if bad_ro else
          f"本地 {len(READONLY_LOCAL)} + 要进程 {len(READONLY_OPEN)}")

    print()
    print("  -- 4. 逐项核对：非只读组 --")
    bad_mut: list[str] = []
    for name, (destroy, idem, open_world) in sorted(MUTATING.items()):
        a = live.get(name)
        if a is None:
            bad_mut.append(f"{name}(不存在)")
            continue
        if a.readOnlyHint is not False:
            bad_mut.append(f"{name}.readOnlyHint={a.readOnlyHint}")
        if a.destructiveHint is not destroy:
            bad_mut.append(f"{name}.destructiveHint={a.destructiveHint}"
                           f"(应为 {destroy})")
        if a.idempotentHint is not idem:
            bad_mut.append(f"{name}.idempotentHint={a.idempotentHint}"
                           f"(应为 {idem})")
        if a.openWorldHint is not open_world:
            bad_mut.append(f"{name}.openWorldHint={a.openWorldHint}"
                           f"(应为 {open_world})")
    check(f"{len(MUTATING)} 个非只读工具标注正确", not bad_mut,
          f"不符 {len(bad_mut)} 项：{bad_mut[:5]}" if bad_mut else
          f"其中破坏性 {sum(1 for v in MUTATING.values() if v[0])} 个")

    # ---------------------------------------------- 5. 蕴含关系
    print()
    print("  -- 5. 蕴含关系：只读 ⇒ 不破坏且无副作用 --")
    contra = [n for n, a in live.items()
              if a.readOnlyHint and a.destructiveHint]
    check("没有「既只读又破坏」的自相矛盾标注", not contra,
          f"矛盾：{contra}" if contra else "0 处")
    contra2 = [n for n, a in live.items()
               if a.readOnlyHint and not a.idempotentHint]
    check("没有「只读但非幂等」的标注", not contra2,
          f"矛盾：{contra2}" if contra2 else "0 处")

    # ---------------------------------------------- 6. 点名钉住
    print()
    print("  -- 6. 点名钉住曾经写错 / 必须危险的标注 --")
    for name, why in sorted(MUST_NOT_BE_READONLY.items()):
        a = live.get(name)
        check(f"{name} 不是只读", a is not None and a.readOnlyHint is False,
              why if a is not None and a.readOnlyHint is False
              else f"**被改回只读了** —— {why}")
    actual_destructive = {n for n, a in live.items() if a.destructiveHint}
    check("破坏性工具集合与预期完全一致",
          actual_destructive == MUST_BE_DESTRUCTIVE,
          f"实际 {sorted(actual_destructive)}"
          if actual_destructive != MUST_BE_DESTRUCTIVE
          else f"{sorted(actual_destructive)}")

    # ---------------------------------------------- 7. _ann 自身会拦矛盾
    print()
    print("  -- 7. `_ann()` 真的会拦住矛盾组合（否则「防矛盾」只是说法）--")
    d = S._ann("测试", read_only=True, open_world=False)
    check("只读工具的 4 位取值正确",
          d == {"title": "测试", "readOnlyHint": True,
                "destructiveHint": False, "idempotentHint": True,
                "openWorldHint": False}, str(d))
    try:
        S._ann("矛盾", read_only=True, destroy=True)
        check("read_only=True + destroy=True 必须抛错", False,
              "**没有抛错** —— 这个设计等于没生效")
    except ValueError as exc:
        check("read_only=True + destroy=True 必须抛错", True, str(exc)[:60])
    except Exception as exc:                              # noqa: BLE001
        check("read_only=True + destroy=True 必须抛错", False,
              f"抛的不是 ValueError 而是 {type(exc).__name__}: {exc}")
    d2 = S._ann("默认", read_only=False)
    check("非只读工具的破坏性默认是保守的 True",
          d2["destructiveHint"] is True,
          "延续 MCP 的保守默认：漏写时按「会破坏」处理")

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
