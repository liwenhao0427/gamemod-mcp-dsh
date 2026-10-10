"""GameMod MCP 服务器：把游戏 Mod 开发能力暴露成标准 MCP 工具。

设计说明
============================================================
本文件是「通用 Agent 接入点」。任何支持 MCP 的客户端
（Claude Code / Codex / Cursor / 自研 Agent）都能通过 stdio
连上这里，获得一整套游戏 Mod 开发工具。

与 AiMod 的关系
------------------------------------------------------------
AiMod 是把「云 LLM + 本地 MCP 工具（26 个）」打包成一个 Tauri 应用，
并且带订阅校验、租约、工具收据签名等商业机制。
本项目取它的**技术思路**（元数据优先定位、运行时探针、增量事件），
但不含任何 AiMod 代码，也不依赖它的云服务：
    · 无云依赖 —— 全部本地计算
    · 无授权校验 —— 装上就能用
    · 工具面按「通用 Agent 的实际需要」重新设计，而不是照抄它的 26 个

工具分层
------------------------------------------------------------
    1. 发现层      : find_game_processes / inspect_game / inspect_binary
    2. 会话层      : open_game / get_session / close_session
    3. 静态查询层  : search_classes / class_detail / search_fields /
                     search_methods / class_hierarchy / find_derived_classes /
                     analyze_dependency / list_images / decompile
    4. 结果句柄层  : read_query_result
    5. 运行时层    : inject_process / launch_game / runtime_info /
                     runtime_class_info / find_instances / find_hook_target /
                     hook_method / hook_address / unhook / hook_status /
                     drain_events / read_memory / write_memory /
                     read_field_value / set_field_value / scan_pattern /
                     eval_script / gc_control
    6. 弱视觉/UI层: capture_screen / ocr / probe_log / wait_for / verify_state
    7. UE 层       : detect_ue / ue_profile / ue_list_entries /
                     ue_script_objects / ue_manifest
    8. 诊断层      : get_diagnostics

关键约定
------------------------------------------------------------
    · 所有工具返回 dict，且**必定**带 `ok` 字段（成功 True / 失败 False）。
    · 失败时带 `error` 字段说明原因，绝不返回看似成功的空数据。
    · 日志一律走 stderr（见 logutil），因为 stdout 是 JSON-RPC 通道。
    · **单次返回不超过 256 KB**，由 `cap.py` 在外层统一兜底（见下）。
      超限时截断最大的顶层列表、完整列表进句柄，返回里会有 `capped: true`
      与 `note`（写明用 `read_query_result` 怎么取回剩下的）。

响应体积上限（P2-4）
------------------------------------------------------------
    每个工具都被 `@_cap_response` 包了一层。为什么不在返回里各写各的：
    38 个工具里只有 25 个走 `_ok()`，而超限最严重的
    find_derived_classes / class_hierarchy / class_detail / runtime_class_info
    恰恰都不走它 —— 想要「一处拦住」就只能包在工具函数外层。
    已有的 `handle` 会被复用（不另造句柄）；`has_more` / `returned`
    一律按**实际**返回改写：明明还有却说没有，比超限更糟。

资源（MCP resources，P2-2）
------------------------------------------------------------
    只有一个：`gamemod://guide`，内容是本 MCP 自己的使用指南
    （与 `skills/gamemod-mod/SKILL.md` 同源）—— 这是全项目唯一
    **没有任何工具出口**的内容，所以放进资源不算重复。
    `get_session` / `open_game` 已经返回引擎能力矩阵，故**不做**
    `gamemod://capabilities`（那会是同一份数据的第二个出口）。
    **不做 prompts**：实测客户端（DSH）的 MCP 客户端实现了
    `resources/list|read`，却完全没有 prompts 相关方法，也没有
    `dsh-mcp-prompts` 这类包 —— 加了也传不到模型面前。
    资源不走 `@_cap_response`（它是工具层的外壳）：内容是受控的静态文件，
    体积远小于上限，验收脚本里卡了这条。

静态与运行时的能力边界（**不许说反**）
------------------------------------------------------------
    静态元数据里有: 类 / 字段 / 方法的**存在与签名**、方法 token
    静态元数据里没有: 字段字节偏移、方法 RVA/绝对地址
    → 偏移与地址**只能**靠注入后的 runtime_class_info / find_hook_target 取。
      class_detail 给出的 token 是连接两侧的钥匙：静态 token 与运行时
      token 相等，才是「Hook 落点正确」的证据。
"""

from __future__ import annotations

import asyncio
import functools
import json
import os
import struct
import sys
import time
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.fastmcp.tools.base import Tool

# 两种启动方式都必须能用（文档与客户端配置里两种都出现过）：
#   1. `python -m gamemod_mcp.server`（配 PYTHONPATH=<repo>/src）
#   2. `python <repo>/src/gamemod_mcp/server.py`
# 第 2 种方式下没有父包，`__package__` 是空串，相对导入会当场炸：
#   ImportError: attempted relative import with no known parent package
# 而这个报错**发生在任何工具注册之前**，客户端只会看到
# `McpError: Connection closed` —— 完全看不出是入口写法的问题。
# 所以这里先把包所在目录（src/）塞进 sys.path，再按包名做绝对导入。
if __package__ in (None, ""):  # 直接当脚本跑
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from gamemod_mcp import __version__, cap
    from gamemod_mcp.engine import decompile as dc
    from gamemod_mcp.engine import discovery, state
    from gamemod_mcp.engine import ue as ue_mod
    from gamemod_mcp.engine.state import ensure_index, ensure_runtime
    from gamemod_mcp.logutil import progress, warn
else:  # 正常作为包被导入
    from . import __version__, cap
    from .engine import decompile as dc
    from .engine import discovery, state
    from .engine import ue as ue_mod
    from .engine.state import ensure_index, ensure_runtime
    from .logutil import progress, warn

# 注意：上面这些导入原本有一部分写在**函数体里**（`from .engine import ue`、
# `from .logutil import warn`）。惰性导入本身没问题，但用相对形式就有问题：
# 以脚本方式启动时没有父包，`from .x import y` 会在**调用那个工具时**才炸
# （initialize 与 tools/list 都能过，看起来一切正常，一用就 ImportError）。
# 入口要同时支持两种形式，所以这些导入都收敛到这里 ——
# 两个模块只依赖标准库与同包模块，提前导入没有额外代价。

# ---------------------------------------------------------------- 服务器实例

mcp = FastMCP(
    "gamemod",
    instructions=(
        "GameMod MCP —— 游戏 Mod 开发的通用工具集。\n"
        "\n"
        "典型工作流（强烈建议按顺序走）：\n"
        "  1. open_game(game_dir=...)  选定游戏并准备好元数据索引\n"
        "  2. search_classes(...)      搜索类，拿到 full_name / nested_name\n"
        "  3. class_detail(...)        细看字段/方法/token/inheritance；\n"
        "                              给 method_name 可只看某个方法的重载\n"
        "  4. inject_process / launch_game  注入运行中的游戏，或启动并注入\n"
        "  5. runtime_class_info(...)  取字段的**真实运行时偏移**与方法真实地址\n"
        "  6. hook_method(...)         在目标方法上装 Hook\n"
        "  7. drain_events()           读取 Hook 捕获到的调用事件\n"
        "\n"
        "核心思路：**先用静态元数据定位，再用运行时确认**。\n"
        "但要注意两侧的能力边界（很容易记反）：\n"
        "  · 静态元数据里有类/字段/方法的存在与签名、以及方法 token；\n"
        "  · 静态元数据里**没有**字段字节偏移，**也没有**方法 RVA/绝对地址；\n"
        "  · 偏移与地址**只能**靠注入后的运行时工具取。\n"
        "  · class_detail 给的 token 是连接两侧的钥匙：静态 token == 运行时 token，\n"
        "    才是「Hook 落点正确」的证据。\n"
        "\n"
        "类名怎么传给下一个工具：**嵌套类型用 nested_name**。\n"
        "`full_name` 对嵌套类型只是短名（如 `State`），机器里有 100 多个同名类型；\n"
        "`nested_name`（如 `Outer.Inner`）才是能唯一定位的那个。\n"
        "\n"
        "注意：第 1~3 步是纯静态查询，**不需要启动游戏**；\n"
        "第 4~7 步需要游戏可注入。**普通用户权限通常就够** ——\n"
        "只要 Agent 与游戏的**完整性级别相同**即可（实测普通权限\n"
        "注入 IL2CPP 游戏成功）；只有游戏以管理员身份运行时，\n"
        "才需要同样以管理员身份运行 Agent。\n"
        "\n"
        "支持三类引擎：IL2CPP / Mono(.NET) / Unreal。**IL2CPP 与 Mono 的配方一致**\n"
        "（探针里是 il2cpp_* 与 mono_* 两套实现，对外的工具与 token 对账方式相同）。\n"
        "Mono 上有四点要知道：方法地址是 JIT 机器码、**没有 RVA**（in_module 为 false）；\n"
        "gc_control 不支持（会如实返回 ok:false，不要当失败重试）；程序集懒加载，\n"
        "注入成功后探针会**等程序集加载稳定再返回**（看 init.assemblies.settled；\n"
        "settled 为 false 时先别 Hook —— 实测过早 Hook 会把游戏打崩）；\n"
        "静态索引默认只收游戏自己的程序集，UnityEngine.* 要用 runtime_class_info\n"
        "在运行时查（class_detail 会说明这一点）。\n"
        "\n"
        "响应体积上限：任何一次返回不超过 256 KB。被截断时返回里会有\n"
        "`capped: true` 与 `note`，照 note 给的\n"
        "read_query_result(handle=..., offset=...) 就能取回剩下的（一条不丢）；\n"
        "此时 `has_more` / `returned` 已按**实际**返回改写，不是原来的请求值。\n"
        "\n"
        "需要完整的使用指南（配方、引擎差异、常见坑）时，读资源\n"
        "gamemod://guide —— 它与 skills/gamemod-mod/SKILL.md 同源。"
    ),
)

# serverInfo.version 默认报的是 **mcp 库自己的版本**（`pkg_version("mcp")`），
# 客户端看到的会是「1.26.0」而不是本项目的版本 —— 排查「Agent 装的是哪个
# 版本的 GameMod MCP」时完全没用（扫描报告 §3.7）。
# FastMCP 没有暴露 version 参数，但底层 `Server.version` 是公开属性，
# 这里补上；拿不到就保持原样，不为了一个显示字段去冒险。
try:
    mcp._mcp_server.version = __version__  # noqa: SLF001 - SDK 未提供公开入口
except Exception as exc:  # noqa: BLE001
    warn(f"设置 MCP serverInfo.version 失败: {exc}")


# ---------------------------------------------------------------- 小工具


def _ok(**kw: Any) -> dict:
    return {"ok": True, **kw}


def _err(msg: str, **kw: Any) -> dict:
    return {"ok": False, "error": msg, **kw}


# ------------------------------------------------------- Hook 参数规格（P1）
#
# `args_type` 以前是「照单全收」的：写错的规格名（例如 `flaot`）会走到探针
# 的 `default: raw.toString()`，于是**悄悄**按指针读一个整型寄存器 ——
# 调用方以为拿到的是 float，实际是个地址字符串。契约第 3 条要求
# 「收下的参数必须真的影响行为」，所以这里显式校验，写错就报错。
#
# float / double 是特例：它们在 x64 上走 XMM 寄存器，探针会从
# `context.xmmN` 取（见 agent.js 的 xmmValue），不要用 `ptr` 去凑。

_ARG_SPECS = ("int", "uint", "long", "ulong", "bool", "float", "double",
              "ptr", "string", "cstring")


def _parse_arg_specs(args_type: str) -> list[str]:
    """解析并校验 `args_type`。非法规格名抛 ValueError（调用方转成 ok:false）。"""
    if not args_type:
        return []
    specs = [s.strip() for s in args_type.split(",") if s.strip()]
    bad = [s for s in specs if s not in _ARG_SPECS]
    if bad:
        raise ValueError(
            f"args_type 里有无法识别的规格：{', '.join(bad)}；"
            f"可用：{'/'.join(_ARG_SPECS)}"
        )
    return specs


def _parse_retval_type(retval_type: str) -> str:
    """校验 `retval_type`（留空表示按静态返回类型自动推断）。"""
    if not retval_type:
        return ""
    v = retval_type.strip()
    if v not in _ARG_SPECS:
        raise ValueError(
            f"retval_type 无法识别：{v}；可用：{'/'.join(_ARG_SPECS)}"
        )
    return v


#: 判断「写入是否撞上 Hook 入口」时给入口留的余量（字节）。
#:
#: Hook 会改写目标入口处的跳转指令（x64 上 5 字节的 `E9 rel32`，Frida 还可能
#: 用更长的绝对跳转），所以命中判定不能只比首地址。
_HOOK_ENTRY_SLACK = 16


def _parse_hex_addr(value: Any) -> int | None:
    """把 `"0x7ffa..."` / 裸十六进制串解析成整数；解析不了给 None。"""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        return int(s, 16)
    except ValueError:
        return None


def _overlapping_hook(st: Any, address: str, size: int) -> dict | None:
    """找出与 `[address, address+size)` 重叠的已装 Hook；没有则 None。

    这是 `write_memory` 的护栏：写已被 Hook 的函数入口会崩游戏（见该工具
    docstring 里的实测记录）。拿不到 Hook 列表时**放行**而不是拦下 ——
    护栏的目的是拦住已知会崩的写法，不是让写入变得不可用。
    """
    if size <= 0:
        return None
    start = _parse_hex_addr(address)
    if start is None:
        return None
    end = start + size
    try:
        res = st.runtime.hook_status()
    except Exception:  # noqa: BLE001 —— 探针不通就不拦
        return None
    for h in (res.get("hooks") or []):
        tgt = _parse_hex_addr(h.get("target"))
        if tgt is None:
            continue
        if start < tgt + _HOOK_ENTRY_SLACK and tgt < end:
            return h
    return None


# ------------------------------------------------------- 响应体积上限（P2-4）
#
# 契约第 5 条「不返回超大结果」以前靠每个工具各自的 `limit` 自觉遵守，
# 但 `limit` 上限是 2000，Agent 完全可以合法地要 2000 条 —— 实测那样一条
# `search_classes('')` 的线上文本是 **856 KB**。现在由 cap.py 统一兜底：
# 超限就截断最大的那个顶层列表，完整列表进句柄，并如实说明剩下的怎么取。
#
# 为什么用装饰器而不是在 `_ok` 里拦：**38 个工具里只有 25 个走 `_ok`**，
# 而超限最严重的 `find_derived_classes` / `class_hierarchy` /
# `class_detail` / `runtime_class_info` 恰恰都不走它。装饰器挂在工具函数上，
# 拿到的是「最终要发出去的 dict」，不会漏。
#
# 装饰器顺序：`@mcp.tool(...)` 在上、`@_cap_response` 在下 —— 于是
# 先套上限再注册，注册进去的就是套好的那个函数。
# `functools.wraps` 保住 `__wrapped__`，`inspect.signature` 会跟着它走，
# 所以**入参 schema 一个字节都不会变**（由 verify_cap.py 逐字比对钉住）。


#: 未套响应上限的**原函数**：`{工具名: 函数}`，在装饰时登记。
#:
#: 用途有两个，都是「必须拿到截断前的真实返回」：
#:   · `verify_response_size.py` 量「截断前有多大」做前后对比；
#:   · `verify_cap.py` 比对「装饰器有没有改变入参签名」。
#:
#: 为什么不直接用 `S.<工具>.__wrapped__`（明明 `functools.wraps` 就给了）：
#: 动态覆盖率垫片（`devtools/_cov/sitecustomize.py`）会在导入完成后把
#: `server` 模块上的工具属性换成它自己的**记录包装器**，那个包装器没有
#: `__wrapped__`。于是脚本会「单独跑全绿、进套件就红」——
#: 本轮真的踩到了（`verify_cap` / `verify_response_size` 都是这样挂的）。
#: 显式登记原函数就不受任何外层替换影响。
RAW_TOOLS: dict[str, Any] = {}


def _cap_response(fn: Any) -> Any:
    """给工具函数套上响应体积上限。"""
    RAW_TOOLS[fn.__name__] = fn

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        payload = fn(*args, **kwargs)
        try:
            return cap.enforce(payload, tool=fn.__name__,
                               store=state.state().cap_results)
        except Exception as exc:      # noqa: BLE001
            # 上限检查是**附加**保护，绝不能因为它自己出问题就让一个
            # 本来正常的工具失败（那会把「省上下文」变成「功能坏了」）。
            # 但也不许静默：打到 stderr，让人看得见。
            warn(f"响应体积上限检查失败，已按原样返回：{type(exc).__name__}: {exc}")
            return payload

    return wrapper


# ------------------------------------------------------- MCP 工具语义标注
#
# MCP 的 `annotations` 有 4 个语义位，**默认值不是「都关」**：
#   readOnlyHint 默认 False、destructiveHint 默认 **True**、
#   idempotentHint 默认 False、openWorldHint 默认 **True**。
# 也就是说：不写 `destructiveHint`，客户端只能按「它会破坏东西」处理；
# 不写 `openWorldHint`，只能按「它与外部世界交互」处理。默认值是**保守**的，
# 但用在本项目上会把 `search_classes`（只读本地 SQLite）也说成危险操作。
# 所以这里**每个**工具都显式表态，不把语义留给默认值。
#
# 几个**曾经写错 / 容易写错**的点（改工具时回头看）：
#   · `drain_events` 曾标 `readOnlyHint=True`，但它是「**取走即消费**」——
#     事件被消费掉就没了，所以是 readOnly=False + destructiveHint=True。
#   · `decompile` / `ue_profile` 给了 output_dir / output_path 就会**落盘**，
#     所以不是只读（但它们只写自己的产物、可覆盖，不算破坏）。
#   · 纯查索引的工具只读本地 SQLite，既不碰游戏进程也不联网
#     → openWorldHint=False；一碰活着的游戏进程就是 True。


def _ann(title: str, *, read_only: bool, open_world: bool = False,
         destroy: bool | None = None,
         idempotent: bool | None = None) -> dict:
    """构造 MCP 工具的 `annotations`，把 4 个语义位写全。

    Args:
        title:      给人看的标题。
        read_only:  不改动任何东西（不写内存、不装 Hook、不落盘、不消费缓冲）。
        open_world: 依赖本进程之外的东西（运行中的游戏进程）。默认 False。
        destroy:    结果不可逆 / 可能让游戏崩 / 数据被消费掉。
                    **只读工具不允许为 True**（会直接抛错，避免自相矛盾的标注）；
                    非只读工具默认 True（沿用 MCP 的保守默认）。
        idempotent: 同样参数再调一次不产生额外影响。只读工具恒为 True
                    （没有任何副作用），非只读默认 False。

    为什么用函数而不是到处写 dict：只读与「不破坏 / 无副作用」是**蕴含关系**，
    写成函数就能在**导入期**就把 `read_only=True` + `destroy=True` 这种
    自相矛盾的组合打出来，而不是等客户端看到怪标注。
    """
    if read_only:
        if destroy is True:
            raise ValueError(
                f"工具「{title}」声明为只读，却又声明 destructiveHint=True —— "
                f"两者矛盾：只读工具不可能破坏任何东西")
        destroy = False
        idempotent = True if idempotent is None else idempotent
    else:
        destroy = True if destroy is None else destroy
        idempotent = False if idempotent is None else idempotent
    return {
        "title": title,
        "readOnlyHint": read_only,
        "destructiveHint": destroy,
        "idempotentHint": idempotent,
        "openWorldHint": open_world,
    }


def _need_index() -> tuple[Any, dict | None]:
    """取当前索引；没有就返回错误响应。"""
    st = state.state()
    if not st.has_index():
        return None, _err(
            "当前会话还没有元数据索引。请先调用 open_game(game_dir=...) "
            "选定游戏，或用 open_game(metadata_path=...) 直接指定元数据文件。",
            session=st.summary(),
        )
    return st.index, None


def _index_query(res: dict | None, what: str, **extra: Any) -> dict:
    """统一收口索引层查询结果。

    索引层用 `found=False` 表示「查了但没这个对象」（而不是抛异常或返回 None）。
    如果直接把这种结果当成成功返回，Agent 会误以为查到了空数据 ——
    这是最容易骗到 Agent 的一类假成功，必须在这里拦掉。

    `extra` 是调用方给的**兜底**提示（如「用 search_classes 先确认类名」），
    只在索引层**没给**同名键时才补上。索引层的提示更具体
    （例如歧义时它会给出 `candidates` 和「用 assembly 消歧」的 hint），
    不能被调用方的泛泛提示覆盖，也不能因为键重名直接抛 TypeError ——
    曾经 `_err()` 因此 `got multiple values for keyword argument 'hint'`。
    """
    if res is None:
        return _err(f"未找到{what}", **extra)
    if not res.get("found", True):
        reason = res.get("reason") or f"未找到{what}"
        payload = {k: v for k, v in res.items() if k not in ("found", "reason")}
        for k, v in extra.items():
            payload.setdefault(k, v)     # 索引层的更具体，优先保留
        return _err(reason, **payload)
    return _ok(**res)


def _same_named_processes(exe: str) -> list[dict]:
    """找出与 `exe` 同映像名的正在运行的进程（按 pid 升序）。

    为什么按**映像名**而不是完整路径比对：拿不到 exe 路径时（权限不足）
    `exe_path` 会是空串，而「名字一样」已经足够触发 Unity 的单实例锁。
    """
    want = Path(exe).name.lower()
    out: list[dict] = []
    try:
        for p in discovery.find_processes(Path(exe).stem, only_games=False):
            name = (getattr(p, "name", "") or "").lower()
            path_name = Path(getattr(p, "exe_path", "") or "").name.lower()
            if name == want or path_name == want:
                out.append({"pid": p.pid, "name": getattr(p, "name", ""),
                            "exe_path": getattr(p, "exe_path", "")})
    except Exception:  # noqa: BLE001 - 平台不支持 / 枚举失败时按「没有」处理
        return []
    return sorted(out, key=lambda d: d["pid"])


def _has_live_session() -> bool:
    """当前是否已经有一个活着的运行时会话（有就不该再 spawn 新进程）。"""
    try:
        return bool(state.state().has_runtime())
    except Exception:  # noqa: BLE001
        return False


def _split_argv(text: str) -> list[str]:
    """按 Windows 习惯拆分命令行参数串。

    规则：空白分隔；**双引号内保留空白**，引号本身去掉；反斜杠**不是**转义符。

    为什么不用 `shlex.split`（本文件曾经的建议）：实测

        shlex.split(r'--path C:\\Games\\My Game\\a.exe --x')
        posix=True （默认）-> ['--path', 'C:GamesMy', 'Gamea.exe', '--x']   # 反斜杠被吃掉
        posix=False        -> ['--path', 'C:\\\\Games\\\\My', 'Game\\\\a.exe', '--x']  # 引号没生效

    默认的 posix 模式把 `\\` 当转义符，而 Windows 路径全是反斜杠 ——
    照那条建议改下去会把 `C:\\Games\\Game\\a.exe` 变成 `C:GamesGamea.exe`，
    是**引进**一个 bug 而不是修掉一个。`posix=False` 又会把引号留在 token 里。
    所以这里自己拆：本项目只跑 Windows，参数就是「空格分隔 + 引号括路径」。

    未闭合的引号按「一直引到串尾」处理（不报错，与多数启动器一致）；
    空的 `""` 会被丢掉（不产生空 token）。
    """
    out: list[str] = []
    cur: list[str] = []
    in_quote = False
    for ch in text:
        if ch == '"':
            in_quote = not in_quote
        elif ch.isspace() and not in_quote:
            if cur:
                out.append("".join(cur))
                cur = []
        else:
            cur.append(ch)
    if cur:
        out.append("".join(cur))
    return out


#: 字段类型名 → 读取宽度（字节）。
#:
#: 为什么需要它：`read_field_value` 原来**固定读 8 字节**。字段只有 4 字节时，
#: 那 4 个「多读的」字节是**相邻字段**的内容 —— 既会给出无意义的
#: `as_int64`（两个字段拼出来的数），又会在字段贴近页尾时把一次**本可成功**
#: 的读取变成假失败。宽度按字段类型来，才不会读到别人的字节。
#:
#: 键同时收 IL2CPP 的 `il2cpp_type_get_name` 形式（`System.Int32`）与短名，
#: 因为运行时返回的是前者；查不到就按 8 字节（**引用类型/指针**在 x64 上
#: 就是 8 字节，且这是原来的行为，不会把没覆盖的类型改坏）。
_FIELD_WIDTH: dict[str, int] = {
    "System.Boolean": 1, "bool": 1,
    "System.Byte": 1, "byte": 1, "System.SByte": 1, "sbyte": 1,
    "System.Char": 2, "char": 2,
    "System.Int16": 2, "short": 2, "System.UInt16": 2, "ushort": 2,
    "System.Int32": 4, "int": 4, "System.UInt32": 4, "uint": 4,
    "System.Single": 4, "float": 4,
    "System.Int64": 8, "long": 8, "System.UInt64": 8, "ulong": 8,
    "System.Double": 8, "double": 8, "System.IntPtr": 8, "System.UIntPtr": 8,
}


def _field_read_width(type_name: Any) -> int:
    """字段类型名 → 要读几个字节。查不到按 8（指针宽度，也是原来的行为）。"""
    if not type_name:
        return 8
    return _FIELD_WIDTH.get(str(type_name).strip(), 8)


#: 字段类型名 → 写值时的「解释方式」。与 `_FIELD_WIDTH` 同一张表的口径，
#: 但**必须区分有符号/无符号/浮点**：同样 4 字节，写成 int 还是 float
#: 得到的字节完全不同，而 `as_int32` 与 `as_float` 在读取时会同时给出，
#: 写的时候却只能选一个 —— 选错的后果是「值看着变了、游戏行为不对」。
_FIELD_WRITE_KIND: dict[str, str] = {
    "System.Boolean": "bool", "bool": "bool",
    "System.SByte": "int", "sbyte": "int",
    "System.Byte": "uint", "byte": "uint",
    "System.Int16": "int", "short": "int",
    "System.UInt16": "uint", "ushort": "uint",
    "System.Int32": "int", "int": "int",
    "System.UInt32": "uint", "uint": "uint",
    "System.Int64": "long", "long": "long",
    "System.UInt64": "ulong", "ulong": "ulong",
    "System.Single": "float", "float": "float",
    "System.Double": "double", "double": "double",
    "System.Char": "uint", "char": "uint",
    "System.IntPtr": "long", "System.UIntPtr": "ulong",
}


def _field_write_kind(field_type: Any, width: int) -> str:
    """字段类型 + 宽度 → 写值方式。

    类型认不出来时**按宽度**给一个保底解释：4 字节按 int、8 字节按 long。
    为什么不按 ptr：写指针是极危险的操作，而「引用类型字段」写指针
    在语义上多半是错的（用户想要的是改内容而不是换对象），
    所以引用类型这里**统一报错**，让调用方改用 `eval_script` 或 Hook。
    """
    key = str(field_type).strip() if field_type else ""
    if key in _FIELD_WRITE_KIND:
        return _FIELD_WRITE_KIND[key]
    if width == 1:
        return "uint"
    if width == 2:
        return "int"
    if width == 4:
        return "int"
    if width == 8:
        return "long"
    return "bytes"


def _encode_field_value(value: Any, width: int, kind: str) -> tuple[bytes | None, str]:
    """把调用方给的值编码成 `width` 字节。返回 `(bytes, error)`。

    抽成**纯函数**是为了能在不启动游戏的情况下验收（§5 要求纯函数与
    端到端两层都验）：编码错了会让「写入成功但值不对」，而那种缺陷
    在 e2e 里很难与「游戏自己覆盖了」区分开。

    规矩：**溢出直接报错，绝不静默截断**。把 300 写进 1 字节字段如果
    截成 44，调用方会看到「ok:true」却拿到一个自己没要的值。
    """
    import struct as _struct

    def _need(cond: bool, msg: str) -> str | None:
        return None if cond else msg

    try:
        if kind == "bytes":
            s = str(value).strip()
            if s.startswith("0x") or s.startswith("0X"):
                s = s[2:]
            s = s.replace(" ", "")
            if len(s) % 2:
                return None, f"字节串长度必须是偶数个十六进制字符：{value!r}"
            try:
                b = bytes.fromhex(s)
            except ValueError:
                return None, f"不是合法的十六进制字节串：{value!r}"
            if len(b) != width:
                return None, (f"字节串长度 {len(b)} 与字段宽度 {width} 不一致"
                              f"（字段类型决定宽度，不能多也不能少）")
            return b, ""

        if kind == "bool":
            if isinstance(value, bool):
                return (b"\x01" if value else b"\x00"), ""
            s = str(value).strip().lower()
            if s in ("1", "true", "yes", "on"):
                return b"\x01", ""
            if s in ("0", "false", "no", "off"):
                return b"\x00", ""
            return None, f"bool 字段只认 true/false/1/0，收到 {value!r}"

        if kind == "float":
            return _struct.pack("<f", float(value)), ""
        if kind == "double":
            return _struct.pack("<d", float(value)), ""

        # 整数族：先按字符串解析，避免 float 传进来被静默截断
        if isinstance(value, bool):
            return None, f"bool 不能写进 {kind} 字段（要写 1/0 请写整数）"
        s = str(value).strip()
        try:
            iv = int(s, 0)  # 支持 0x 前缀
        except ValueError:
            return None, f"{kind} 字段要整数，收到 {value!r}"

        limits = {
            "int": (-(1 << 7), (1 << 7) - 1, "b", 1),
            "uint": (0, (1 << 8) - 1, "B", 1),
            "long": (-(1 << 63), (1 << 63) - 1, "q", 8),
            "ulong": (0, (1 << 64) - 1, "Q", 8),
        }
        # 1/2/4 字节统一走 int/uint 的宽度表
        width_limits = {
            (1, "int"): (-128, 127), (1, "uint"): (0, 255),
            (2, "int"): (-32768, 32767), (2, "uint"): (0, 65535),
            (4, "int"): (-(1 << 31), (1 << 31) - 1),
            (4, "uint"): (0, (1 << 32) - 1),
        }
        if kind in ("int", "uint") and (width, kind) in width_limits:
            lo, hi = width_limits[(width, kind)]
        elif kind in limits:
            lo, hi = limits[kind][0], limits[kind][1]
        else:
            return None, f"不支持的值类型：{kind}"
        if not (lo <= iv <= hi):
            return None, (f"{iv} 超出 {kind}({width} 字节) 的范围 [{lo}, {hi}]；"
                          f"溢出不会被静默截断")
        fmt = {1: ("b" if kind == "int" else "B"),
               2: ("h" if kind == "int" else "H"),
               4: ("i" if kind == "int" else "I"),
               8: ("q" if kind in ("long", "int") else "Q")}[width]
        return _struct.pack("<" + fmt, iv), ""
    except (TypeError, ValueError, OverflowError) as exc:
        return None, f"编码失败：{exc}"


# ================================================================
# 1. 发现层
# ================================================================


@mcp.tool(annotations=_ann("列出运行中的游戏进程", read_only=True, open_world=True))
@_cap_response
def find_game_processes(name_filter: str = "", only_games: bool = True) -> dict:
    """列出正在运行的游戏进程，并识别其运行时（il2cpp / mono / 其它）。

    Args:
        name_filter: 进程名子串过滤（大小写不敏感）。
        only_games:  只要看起来像游戏的进程（默认 True）。

    Returns:
        {ok, count, processes: [{pid, name, exe_path, window_title,
                                 runtime, is_64}]}
        `is_64` 为 None 表示**没测出来**（拿不到进程句柄），不是「32 位」。
    """
    try:
        procs = discovery.find_processes(name_filter=name_filter,
                                         only_games=only_games)
    except Exception as exc:
        return _err(f"枚举进程失败：{exc}")

    return _ok(
        count=len(procs),
        processes=[
            {
                "pid": p.pid,
                "name": p.name,
                "exe_path": p.exe_path,
                "window_title": p.window_title,
                "runtime": p.runtime,
                "is_64": p.is_64,
            }
            for p in procs
        ],
    )


@mcp.tool(annotations=_ann("探测游戏安装目录", read_only=True))
@_cap_response
def inspect_game(game_dir: str) -> dict:
    """探测一个游戏目录：识别引擎、找出主程序与关键文件。

    本项目的使用方式是「一次只针对一个游戏项目目录」，
    所以不必去扫全机有哪些游戏 —— 直接把目录传进来即可。

    Args:
        game_dir: 游戏安装目录。

    Returns:
        {ok, engine, main_exe, il2cpp/mono/ue 的关键路径, ...}
    """
    if not game_dir:
        return _err("必须提供 game_dir")
    try:
        info = discovery.describe_game(game_dir)
    except Exception as exc:
        return _err(f"探测失败：{exc}", game_dir=game_dir)
    if not info.get("exists"):
        return _err(f"目录不存在：{game_dir}")
    return _ok(**info)


@mcp.tool(annotations=_ann("分析可执行文件结构", read_only=True))
@_cap_response
def inspect_binary(path: str) -> dict:
    """分析 PE 文件（exe/dll）：架构、节区、导出表、导入表、运行时类型。

    用于判断游戏是否加壳、是否托管、以及找出可 Hook 的导出函数。

    关键结论（architecture / runtime / export_count / section_count）
    会**同时**提到顶层与 `pe` 子对象里：Agent 通常只关心「这是不是 x64」
    「这是不是 .NET」「有没有导出函数」，不该为了拿这几个值多下钻一层。
    完整的节区表、导入 DLL 列表留在 `pe` 里。

    Args:
        path: 可执行文件或 DLL 路径。
    """
    res = state.inspect_binary(path)
    if not res.get("ok"):
        return res
    pe = res.get("pe") or {}
    # 摘掉几个最常被问到的字段提到顶层（保留 pe 里的原件，不做搬移）
    for key in ("size", "machine", "is_64", "image_base", "entry_point_rva",
                "subsystem", "runtime", "is_dotnet", "is_nativeaot",
                "section_count", "export_count", "import_dll_count"):
        if key in pe:
            res.setdefault(key, pe[key])
    return res


# ================================================================
# 2. 会话层
# ================================================================


@mcp.tool(annotations=_ann("选定游戏并准备索引", read_only=False, destroy=False, idempotent=True))
@_cap_response
def open_game(
    game_dir: str = "",
    metadata_path: str = "",
    engine: str = "",
    cache_dir: str = "",
    force_rebuild: bool = False,
) -> dict:
    """选定游戏，并构建（或复用）它的元数据索引（**跨引擎自动识别**）。

    之后所有查询工具都以这个游戏为上下文。切换游戏会重置会话
    （已注入的运行时会被安全卸载）。

    支持三种引擎，会自动识别：
      · IL2CPP — 元数据是 global-metadata.dat，逻辑在 GameAssembly.dll
      · Mono   — 元数据是托管 dll（通常是 Assembly-CSharp.dll）
      · UE     — 无集中元数据（能力以 get_diagnostics 报告为准）

    重复调用同一个游戏**不会重建**（返回 reused=true，通常 10ms 内返回）。

    Args:
        game_dir:      游戏安装目录（推荐；会自动识别引擎并定位元数据文件）。
        metadata_path: 直接指定元数据文件（IL2CPP 用 global-metadata.dat，
                       Mono 用某个 .dll；目录结构不标准时用）。
        engine:        强制指定引擎（il2cpp / mono / ue），一般不用填。
        cache_dir:     索引缓存目录，默认 ~/.gamemod/cache。
        force_rebuild: 强制重建索引（怀疑缓存损坏时用）。

    Returns:
        {ok, engine, engine_label, metadata_path, index: {type_count,
         method_count, field_count, capabilities...}, elapsed, reused}
    """
    # **两阶段（缺陷 #3 的修复）**：先把目标算清楚（只读），把「引擎受不受支持」
    # 这类判定做完，确认没问题才提交会话身份。
    #
    # 为什么不能沿用「先 select_game 再判断」：`select_game` 会先收拾旧会话并
    # 写入新身份，而下面这段引擎判定是在它**之后**做的 —— 于是
    # `open_game("E:\\project")`（容器目录）虽然如实返回 ok:false，
    # 会话却已经被改成了 `E:\project`，`exe` 还被填成子目录里某个游戏的主程序。
    # 而 `launch_game()` 不传 exe 时默认用会话里的 exe ——
    # 等于会去启动容器下的某一款游戏（扫描报告 §3.1 的最坏情况）。
    planned = state.plan_selection(game_dir=game_dir,
                                   metadata_path=metadata_path, engine=engine)
    if not planned.get("ok"):
        return planned

    if planned.get("reused"):
        # 同一个游戏、索引已在：身份不动，直接复用。
        # `force_rebuild` 交给 ensure_index 自己判（它会重建而不是复用）。
        res = ensure_index(cache_dir=cache_dir, force_rebuild=force_rebuild)
        if not res.get("ok"):
            return res
        st = state.state()
        res["engine"] = st.engine
        res["engine_label"] = state._engine_label(st.engine)
        return res

    plan = planned["plan"]
    eng = str(plan.get("engine") or "").lower()
    gd = plan.get("game_dir") or game_dir

    # 「会话该不该切过去」与「能不能建索引」是**两件事**，必须分开判：
    #
    #   · 引擎受支持（il2cpp / mono / ue）→ 会话**应该**切过去。
    #     对 UE 来说这一点尤其重要：`open_game` 会因为「没有集中元数据」而返回
    #     ok:false，但会话身份必须已经指向这个 UE 游戏 —— 否则随后的
    #     `detect_ue()` / `ue_profile()` 不传 exe 时找不到目标
    #     （它们从会话里取 exe，见 `_ue_exe`）。文档里那句「会话已按这个游戏
    #     选好」说的就是这件事。
    #   · 引擎**不受支持**（unknown / unity / Godot 等）→ 会话**不许**动。
    #     这正是缺陷 #3：以前先提交再判定，于是 `open_game("E:\project")`
    #     虽然如实返回 ok:false，会话却已经被改成容器目录、
    #     exe 还被填成子目录里某个游戏的主程序 —— 而 `launch_game()` 不传参时
    #     默认就用会话里的 exe（扫描报告 §3.1 的最坏情况）。
    supported = eng in ("il2cpp", "mono", "ue")

    if not supported:
        # ---- 不受支持：**一个字节都不写进会话**，只如实报错 ----
        info: dict[str, Any] = {}
        try:
            info = discovery.describe_game(gd)
        except Exception as exc:  # noqa: BLE001 - 提示信息拿不到也不能再抛
            warn(f"补充探测失败: {exc}")
        hint = info.get("hint", "")
        # 把「会话未被触碰」显式带出去，Agent 才不会以为「失败了但游戏换过去了」
        untouched = {"session_unchanged": True,
                     "session": state.state().summary()}
        if eng == "unity":
            return _err(
                "识别为 Unity，但既没有 IL2CPP 的 global-metadata.dat、也没有 "
                "Mono 的 Managed 程序集（可能是被裁剪的构建或被删过文件）；"
                "若元数据在别处，请用 metadata_path 直接指定。",
                engine=eng, game_dir=gd, hint=hint, **untouched,
            )
        return _err(
            "没能识别出可支持的引擎（本 MCP 只支持 IL2CPP / Mono / UE）。"
            + (f" {hint}" if hint else ""),
            engine=eng or "unknown",
            game_dir=gd,
            hint=hint,
            container=info.get("container"),
            engine_family=info.get("engine_family"),
            # 「外面套了一层」的情况：直接给出该指向哪里，省掉一轮试错
            game_root=info.get("game_root"),
            **untouched,
        )

    # ---- 引擎受支持：现在提交会话身份 ----
    sel = state.commit_selection(plan)

    # UE 没有集中元数据，「建索引」这一步不适用 —— 但**会话已经切好了**，
    # 所以这里要给出 ue_* 的出口，而不是让调用方去翻 metadata_path。
    if not metadata_path and eng == "ue":
        info = {}
        try:
            info = discovery.describe_game(gd)
        except Exception as exc:  # noqa: BLE001
            warn(f"补充探测失败: {exc}")
        return _err(
            "Unreal 游戏没有集中元数据，不需要（也无法）建索引 —— "
            "请直接使用 ue_* 工具：detect_ue / ue_profile / ue_list_entries / "
            "ue_script_objects / ue_manifest（会话已按这个游戏选好）。",
            engine="ue",
            engine_label=sel.get("engine_label", ""),
            game_dir=gd,
            ue=info.get("ue", {}),
            next_tools=["detect_ue", "ue_profile", "ue_list_entries",
                        "ue_script_objects", "ue_manifest"],
            session=sel,
        )

    res = ensure_index(cache_dir=cache_dir, force_rebuild=force_rebuild)
    if not res.get("ok"):
        return res
    # 把「这个引擎能提供什么」显式带出来，Agent 就不会把
    # 「IL2CPP 元数据里没有字段偏移」误判成工具坏了。
    res["engine"] = sel.get("engine", "")
    res["engine_label"] = sel.get("engine_label", "")
    return res


@mcp.tool(annotations=_ann("查看当前会话状态", read_only=True))
@_cap_response
def get_session() -> dict:
    """查看当前会话状态：选中的游戏、索引统计、运行时注入情况。

    排查「为什么查不到类 / Hook 不上」时，先看这个。
    """
    st = state.state()
    return _ok(**st.summary())


@mcp.tool(annotations=_ann("关闭会话", read_only=False, destroy=False, idempotent=True))
@_cap_response
def close_session(close_runtime: bool = True, close_index: bool = True) -> dict:
    """关闭当前会话：卸载所有 Hook、断开注入、关闭索引。**只有真的恢复原状才 `ok=true`**。

    任一步失败会 `ok=false` + `error`，并**保留会话**以便重试 —— 这是有意的：
    本工具的语义是「游戏已恢复原状」，一个假成功会让你带着仍被篡改的游戏收工。

    做完了就调用它，把游戏恢复原状（卸载 Hook 后游戏行为回到未修改状态）。

    **只有真的恢复成功才返回 `ok: True`。** 卸载 Hook / 卸载脚本 / detach
    任何一步失败都会 `ok: False` + `error`，并保留会话以便重试 ——
    这是有意为之：本工具的语义是「游戏已恢复原状」，
    一个假成功会让 Agent 带着仍被篡改的游戏进程收工。
    """
    st = state.state()
    out: dict[str, Any] = {}
    failures: list[str] = []
    if close_runtime:
        rt = st.close_runtime()
        out["runtime"] = rt
        if not rt.get("ok"):
            failures.append(str(rt.get("error") or "运行时会话关闭失败"))
        if rt.get("wedged"):
            # 探针卡死：**没**恢复原状（Hook 可能还在游戏里），但会话已释放。
            # 这两件事必须分开说 —— 早先只把 `restored: False` 放进 runtime
            # 子对象，顶层却照旧 `restored: True`，自相矛盾（实测发现）。
            out["wedged"] = True
            out["session_released"] = bool(rt.get("session_released"))
    if close_index:
        ix = st.close_index()
        out["index"] = ix
        if not ix.get("ok"):
            failures.append(str(ix.get("error") or "索引关闭失败"))
    if failures:
        out["restored"] = False
        return _err("；".join(failures), **out)
    out["restored"] = True
    return _ok(**out)


# ================================================================
# 3. 静态查询层
# ================================================================


@mcp.tool(annotations=_ann("搜索类/结构/枚举", read_only=True))
@_cap_response
def search_classes(
    pattern: str = "",
    offset: int = 0,
    limit: int = 50,
    subtype_of: str = "",
    assembly: str = "",
    only_enums: bool = False,
    exclude_compiler_generated: bool = True,
) -> dict:
    """搜索游戏里的类、结构体、枚举 —— Mod 开发最主要的入口。支持按基类与程序集过滤。

    返回结果里带 `handle`，结果集较大时可用 read_query_result 翻页，
    并可用它的 `pattern` 参数在结果内再过滤。

    **下游工具该用哪个名字**：用 `nested_name`，不要用 `full_name`。
    嵌套类型（状态机 `State`、协程体 `<Xxx>d__0`、私有内部类）的
    `full_name` 只是**短名**（IL2CPP 的 namespace 只属于最外层类型），
    实测 20,072 个类型里有 131 个叫 `State`，只有 1 个是顶层。
    拿短名去查，会命中一堆同名类型，或直接被判为歧义而拒绝。
    `nested_name`（如 `Outer.Inner`）才是能唯一定位的那个。

    Args:
        pattern:    名称子串（大小写不敏感）。空表示不过滤。
        subtype_of: 只要继承自该类型的（如 "UnityEngine.MonoBehaviour"）。
        assembly:   按程序集名过滤（子串，如 "TeamTapas"）。
        only_enums: 只要枚举（找游戏常量表很有用）。
        limit:      本页最多返回多少条（上限 2000）。

    Returns:
        {ok, handle, total, returned, has_more, items: [{full_name, nested_name,
         namespace, method_count, field_count, is_enum, image, token}]}
    """
    idx, err = _need_index()
    if err:
        return err
    try:
        res = idx.search_classes(
            pattern=pattern, offset=offset, limit=limit,
            subtype_of=subtype_of, assembly=assembly, only_enums=only_enums,
            exclude_compiler_generated=exclude_compiler_generated,
        )
        return _ok(**res)
    except Exception as exc:
        return _err(f"搜索失败：{exc}")


@mcp.tool(annotations=_ann("查看类的完整细节", read_only=True))
@_cap_response
def class_detail(full_name: str, include_inherited: bool = False,
                 method_name: str = "", assembly: str = "") -> dict:
    """查看一个类的完整信息：字段（含类型）、方法（含 token）、继承链；给 `method_name` 可只看某方法的重载。

    **这是定位 Hook 落点的核心工具**：方法的 token 由这里给出，
    再配合 runtime_class_info / find_hook_target 就能拿到真实的运行时地址。

    重要：**静态元数据里没有字段偏移、也没有方法地址**（只有 token）。
    字段偏移与方法真实地址必须靠注入后的运行时工具取。

    同类名跨程序集时会**拒绝并列出候选**（`ok:false` + `candidates`），
    请改用完整名或补 `assembly=` —— 因为返回的是「这个类的全部字段表」，
    选错类不是少几条数据，而是整份结果都是别的类的。

    查**嵌套类型**时，`full_name` 传 search_classes 给的 `nested_name`
    （如 `Outer.Inner`）：嵌套类型的 `full_name` 只是短名，多个类会撞名，
    本工具会因歧义而拒绝（这不是故障，是防止你拿到另一个类的字段表）。

    Args:
        full_name:         类的完整名（如 "PresetLoaderV2"、
                           "TeamTapas.ProjectR.UserDatas.Service.UserData_Service"，
                           或嵌套类型的 "Outer.Inner"）。
        include_inherited: 是否包含继承来的字段/方法。
        method_name:       只关心某个方法时填这里。填了就只返回该方法的
                           全部重载（含 token / 参数表 / 是否静态），
                           等价于旧的 get_method_source，省一轮往返。
        assembly:          同名类消歧用（子串匹配，如 "TeamTapas"）。留空则
                           名字必须唯一，否则报错并给出候选。

    Returns:
        {ok, full_name, nested_name, namespace, image, token, ..., fields,
         methods, parent, base_chain, derived_count, derived, derived_returned,
         derived_has_more}
        `derived_count` 是**派生类总数**（真值）；`derived` 只是前 50 个名字，
        被截断时 `derived_has_more` 为 True —— 需要完整列表请用
        `find_derived_classes`。
    """
    idx, err = _need_index()
    if err:
        return err
    if not full_name:
        return _err("必须提供 full_name")
    # 指定 method_name 时强制带上继承的方法：被合并的 get_method_source
    # 原本就是 include_inherited=True，若不这样，查一个继承来的
    # Update/OnEnable 会得到「未找到」的假否定。
    want_inherited = include_inherited or bool(method_name)
    try:
        res = idx.class_detail(full_name, include_inherited=want_inherited,
                               assembly=assembly)
    except Exception as exc:
        return _err(f"查询失败：{exc}")

    # 只要某个方法时，在这里收窄 —— 合并 get_method_source 的关键：
    # 它原本就是「class_detail 结果里按名字过滤 methods」，独立成一个工具
    # 只会让 Agent 犹豫该调哪个。
    if method_name:
        if not res.get("found"):
            return _index_query(
                res, f"方法：{full_name}.{method_name}",
                hint="用 search_classes 先确认类名",
            )
        all_methods = res.get("methods") or []
        hits = [x for x in all_methods if x.get("name") == method_name]
        if not hits:
            return _err(
                f"类 {full_name} 中未找到方法 {method_name}",
                available=sorted({x.get("name", "") for x in all_methods})[:80],
                overload_count=0,
            )
        out = _ok(
            class_full_name=res.get("full_name"),
            method=method_name,
            overload_count=len(hits),
            methods=hits,
        )
        if len(hits) == 1:
            # 唯一匹配：把常用字段提上来，避免 Agent 写 methods[0].token
            for k in ("index", "name", "token", "return_type",
                      "parameter_count", "parameters", "is_static",
                      "is_virtual", "is_constructor", "flags"):
                if k in hits[0]:
                    out.setdefault(k, hits[0][k])
        else:
            out["note"] = (
                f"{method_name} 有 {len(hits)} 个重载，请按 token 选定目标"
                f"（把对应 token 传给 hook_method 的 token 参数）；"
                f"hook_method 的 args_type 也要与所选重载的参数对应。"
            )
        return out

    return _index_query(
        res, f"类：{full_name}",
        hint="用 search_classes 先确认名称；注意有些类型需要完整命名空间",
    )


@mcp.tool(annotations=_ann("搜索字段", read_only=True))
@_cap_response
def search_fields(
    pattern: str,
    offset: int = 0,
    limit: int = 100,
    owner: str = "",
    assembly: str = "",
) -> dict:
    """按名字搜索字段，返回所属类与字段类型。**按字段名搜**（定位「金币」「血量」这类目标的首选入口）。

    `total` 是**真实匹配总数**（不受 limit 影响），配合 `offset`/`limit` 翻页。
    `owner` 限定单个类；同名类跨程序集时会**拒绝并给候选**，可用 `assembly=` 消歧。

    Args:
        pattern: 字段名子串（大小写不敏感）。
        offset:  跳过前多少条（配合 total / has_more 翻页）。
        limit:   本页最多返回多少条（上限 2000）。
        owner:   限定在某一个类里搜。接受完整类名或短名，但**短名必须唯一**：
                 若命中多个同名类（常见于不同程序集），本工具会拒绝并返回
                 candidates，此时请改用完整类名或用 assembly 消歧。
                 为什么不猜：把两个无关类的字段混在一起返回，ok 仍是 true，
                 但算出来的偏移必然错，且要到运行时才暴露。
        assembly: 程序集名子串，仅用于在 owner 命中多个类时消歧。

    Returns:
        {ok, handle, total, offset, limit, returned, has_more, items}
        `total` 是**真实匹配总数**；若超过句柄上限会带 handle_truncated
        与 handle_note（翻页请用本工具的 offset，不要依赖句柄拿全集）。
    """
    idx, err = _need_index()
    if err:
        return err
    if not pattern:
        return _err("必须提供 pattern")
    try:
        res = idx.search_fields(pattern=pattern, offset=offset, limit=limit,
                                owner=owner, assembly=assembly)
    except Exception as exc:
        return _err(f"搜索失败：{exc}")
    # 索引层在 owner 查不到/有歧义时返回 {"error": ...} 而不是结果集；
    # 若直接 _ok(**res) 就会变成 ok=true + error=... 的假成功。
    if "error" in res:
        return _err(res["error"], **{k: v for k, v in res.items() if k != "error"})
    return _ok(**res)


@mcp.tool(annotations=_ann("搜索方法", read_only=True))
@_cap_response
def search_methods(
    pattern: str,
    offset: int = 0,
    limit: int = 100,
    owner: str = "",
    assembly: str = "",
) -> dict:
    """按名字搜索方法，返回所属类、返回类型与 token。`owner` 语义与 `search_fields` 一致。

    Args:
        pattern: 方法名子串（大小写不敏感）。
        offset:  跳过前多少条（配合 total / has_more 翻页）。
        limit:   本页最多返回多少条（上限 2000）。
        owner:   限定在某一个类里搜，语义与 search_fields 一致
                 （短名必须唯一，否则拒绝并给 candidates）。
        assembly: 程序集名子串，仅用于 owner 消歧。

    Returns:
        {ok, handle, total, offset, limit, returned, has_more, items}
        `total` 是真实匹配总数。
    """
    idx, err = _need_index()
    if err:
        return err
    if not pattern:
        return _err("必须提供 pattern")
    try:
        res = idx.search_methods(pattern=pattern, offset=offset, limit=limit,
                                 owner=owner, assembly=assembly)
    except Exception as exc:
        return _err(f"搜索失败：{exc}")
    if "error" in res:
        return _err(res["error"], **{k: v for k, v in res.items() if k != "error"})
    return _ok(**res)


@mcp.tool(annotations=_ann("查看类继承关系", read_only=True))
@_cap_response
def class_hierarchy(full_name: str, direction: str = "both", depth: int = 6,
                    assembly: str = "") -> dict:
    """查看类的继承体系（向上找基类、向下找派生类）。`direction="down"` 是**逐层**的：`depth` 是所在层、`parent` 是**真实**直接父类；单层超 500 条时 `down_has_more=true`。

    同类名跨程序集时会拒绝并给出候选（与 `class_detail` 同一规则）。
    （早期版本把传递式后代当成直接子类，`depth`/`parent` 是错的 —— 现在两者用途已分开，
    想一次捞全所有脚本请用 `find_derived_classes`。）

    Args:
        full_name: 类完整名。
        direction: up / down / both。
        depth:     向下递归深度。
        assembly:  同名类消歧用（子串匹配，如 "TeamTapas"）。
    """
    idx, err = _need_index()
    if err:
        return err
    try:
        res = idx.class_hierarchy(full_name, direction=direction, depth=depth,
                                  assembly=assembly)
    except Exception as exc:
        return _err(f"查询失败：{exc}")
    return _index_query(res, f"类：{full_name}")


@mcp.tool(annotations=_ann("查找继承自某类的类型", read_only=True))
@_cap_response
def find_derived_classes(base_full_name: str, limit: int = 200,
                         assembly: str = "") -> dict:
    """找出所有继承自某个基类的类型（含间接继承）；配 `assembly=` 可筛出游戏自己的 MonoBehaviour 脚本（**找脚本的必备工具**）。

    `total` 是匹配总数，被 `limit` 截断时 `has_more=true`。

    两个最常见的用法：

    · 找游戏自己写的 MonoBehaviour 脚本（Mod 最常 Hook 的目标）：
      `find_derived_classes(base_full_name="UnityEngine.MonoBehaviour",
                            assembly="Assembly-CSharp")`
      传 assembly 比按程序集名前缀猜「哪些是引擎自带的」可靠。
    · 找所有 UI 面板 / 数据服务：把 base_full_name 换成对应基类即可。

    基类**不必**在当前索引的程序集里（如 UnityEngine.MonoBehaviour
    属于框架程序集），这种情况结果依然有效，并在 note 里如实说明。

    `total` 是**匹配总数**，不受 limit 影响；`returned` 才是本次返回条数。
    若 `has_more` 为 True，说明被 limit 截断了，还有结果没返回。

    Args:
        base_full_name: 基类完整名，如 "UnityEngine.MonoBehaviour"。
        assembly:       只看该程序集（子串匹配，如 "TeamTapas"）。留空不限。
        limit:          最多返回多少条（`total` 仍是匹配总数）。

    Returns:
        {ok, found, base, total, returned, has_more, items: [{index,
         full_name, image, field_count, method_count}]}
    """
    idx, err = _need_index()
    if err:
        return err
    try:
        res = idx.find_derived_classes(base_full_name, limit=limit,
                                       assembly=assembly)
    except Exception as exc:
        return _err(f"查询失败：{exc}")
    return _index_query(res, f"基类：{base_full_name}")


@mcp.tool(annotations=_ann("分析类型依赖", read_only=True))
@_cap_response
def analyze_dependency(full_name: str, assembly: str = "") -> dict:
    """分析一个类型引用了哪些其它类型（字段/父类/方法返回类型）。**近似，非精确调用图**。

    `referenced_count` 是真实总数（不受 limit 影响）；
    `referenced_types` 最多 100 条并带 `referenced_has_more`。

    用来判断「改这个类会不会牵连很多地方」。

    同类名跨程序集时会拒绝并给出候选（与 `class_detail` 同一规则）。

    Args:
        full_name: 类完整名。
        assembly:  同名类消歧用（子串匹配，如 "TeamTapas"）。
    """
    idx, err = _need_index()
    if err:
        return err
    try:
        res = idx.analyze_dependency(full_name, assembly=assembly)
    except Exception as exc:
        return _err(f"分析失败：{exc}")
    return _index_query(res, f"类：{full_name}")


@mcp.tool(annotations=_ann("列出程序集", read_only=True))
@_cap_response
def list_images(pattern: str = "", offset: int = 0, limit: int = 200) -> dict:
    """列出游戏里的程序集（dll）及各自的类型数量。支持 `offset`/`limit` 翻页并如实给 `has_more`。

    用来判断「游戏逻辑主要在哪个 dll」，从而缩小搜索范围。

    Args:
        pattern: 名称子串过滤（如 "TeamTapas"）。
        offset:  从第几条开始（翻页用）。
        limit:   本页最多几条。被截断时 `has_more` 为 true。
    """
    idx, err = _need_index()
    if err:
        return err
    try:
        offset = max(0, offset)
        limit = max(1, min(limit, 2000))
        # total 用 COUNT(DISTINCT ...) 单独算，是真值；
        # items 才受 limit/offset 限制。以前只给 total 与 returned、
        # 不给 has_more 也没有 offset，调用方既无法确认被截断，也无法翻页。
        total = idx.conn.execute(
            "SELECT COUNT(DISTINCT image) FROM types WHERE image LIKE ?",
            (f"%{pattern}%",),
        ).fetchone()[0]
        rows = idx.conn.execute(
            "SELECT image, COUNT(*) AS n FROM types "
            "WHERE image LIKE ? GROUP BY image ORDER BY n DESC LIMIT ? OFFSET ?",
            (f"%{pattern}%", limit, offset),
        )
        items = [{"image": r["image"], "type_count": r["n"]} for r in rows]
        out = _ok(total=total, offset=offset, limit=limit,
                  returned=len(items), has_more=offset + len(items) < total,
                  items=items)
        if pattern and total == 0:
            out["note"] = (f"没有任何程序集名匹配 {pattern!r}；"
                           f"去掉 pattern 可看全部程序集。")
        return out
    except Exception as exc:
        return _err(f"查询失败：{exc}")


@mcp.tool(annotations=_ann("反编译 Mono 程序集", read_only=False, destroy=False, idempotent=True))
@_cap_response
def decompile(assembly: str = "", type_name: str = "", output_dir: str = "",
              split_files: bool = True) -> dict:
    """反编译 Mono/.NET 程序集，拿到**真正的 C# 源码**（薄封装 `ilspycmd`）。

    给 `type_name` 反编译单个类型（小结果内联在 `source`）；给 `output_dir`
    反编译整个程序集（只落盘 + 返回文件清单，**不内联源码**）。
    **嵌套类型会退让到最外层**并写 `nested_note`，`decompiled_type` 如实报
    **实际产出**的类型名。**只支持 Mono**，IL2CPP 会明确报错。
    依赖是**可选**的，见 `get_diagnostics` 的 `decompiler.available`。

    为什么需要它：静态元数据只能给出「有哪些类/字段/方法、签名如何」，
    但给不出**方法体逻辑**。想知道「这个方法到底怎么算伤害的」，
    就要反编译看源码 —— 这是元数据索引与运行时 Hook 都替代不了的一环。

    **只支持 Mono/.NET**。IL2CPP 的 GameAssembly.dll 里没有托管元数据，
    传进来会明确报错（IL2CPP 的 C# 反编译需要先造 DummyDll，本项目暂未实现）。

    两种粒度，用参数区分（故意只做一个工具，避免让你猜该用哪个）：

        · 反编译**一个类型**：给 type_name（可选 output_dir）
          源码小于 20 万字符时直接内联在 `source` 里返回。
        · 反编译**整个程序集**：只给 output_dir
          产物可能上万文件，因此只落盘 + 返回文件清单统计，**不内联源码**。

    Args:
        assembly:   程序集路径（.dll / .exe）。Mono 游戏通常是
                    `<游戏目录>/<游戏名>_Data/Managed/Assembly-CSharp.dll`。
                    留空则用当前会话识别出的那个（Mono 游戏可直接用）。
        type_name:  类型名，支持 `Namespace.Type`。传空表示反编译整个程序集。
                    嵌套类型可传 `Outer.Inner` 或 `Outer+Inner`：
                    由于 ilspycmd 无法单独定位嵌套类型（产物始终是最外层那一份），
                    会自动退让到**最外层**类型，并在 `nested_note` 里说明。
                    `decompiled_type` 报的是**实际产出的**类型名（即最外层），
                    不是请求名 —— 这样调用方不会把外层源码误当成内层源码。
                    `declared_types` 已核对，内层类型的声明确实在这份产物里。
        output_dir: 输出目录。给了就落盘（整程序集模式**必填**）。
        split_files: 整程序集模式下是否一个类型一个文件（默认 True）。
                    True 出 800+ 个小文件（实测 3.5 秒），便于按需读取；
                    False 出单个大文件（实测 11.5 秒），便于整体搜索。
    """

    probe = dc.available()
    if not probe["available"]:
        return _err(probe["hint"])

    # 程序集留空 → 用当前会话识别出的那个（Mono 会话里就是 Assembly-CSharp.dll）
    asm = assembly
    if not asm:
        sess = state.state().summary()
        asm = (sess.get("metadata_path") or "")
        if not asm:
            return _err(
                "未指定 assembly，且当前会话也没有已打开的程序集。"
                "请传 assembly 路径，或先调用 open_game 选定游戏。"
            )
        if not str(asm).lower().endswith((".dll", ".exe")):
            return _err(
                f"当前会话的元数据不是 .NET 程序集（{asm}），无法反编译。"
                "decompile 只支持 Mono/.NET；IL2CPP 需要先造 DummyDll。"
            )

    try:
        if type_name:
            res = dc.decompile_type(asm, type_name, output_dir)
        elif output_dir:
            res = dc.decompile_assembly(asm, output_dir, split_files)
        else:
            return _err(
                "必须指定 type_name（反编译单个类型）或 output_dir"
                "（反编译整个程序集），两者至少要有一个。"
            )
    except Exception as exc:
        return _err(f"反编译失败：{exc}")

    if not res.get("ok"):
        return _err(res.get("error") or "反编译失败", **{
            k: v for k, v in res.items() if k not in ("ok", "error")
        })
    return _ok(**{k: v for k, v in res.items() if k != "ok"})


# ================================================================
# 4. 结果句柄层
# ================================================================


@mcp.tool(annotations=_ann("翻页/过滤读取查询结果", read_only=True))
@_cap_response
def read_query_result(handle: str, offset: int = 0, limit: int = 50,
                      pattern: str = "") -> dict:
    """按句柄翻页读取之前某次查询的完整结果；给了 `pattern` 就在结果内再过滤。

    列表类工具单次只返回一页，但完整结果留在句柄里。
    （原先独立的 `search_query_result` 已按契约第 3 条合并进来 ——
    它就是本工具在 `pattern=""` 时的特例。）
    两个用途共用本工具：

    - 只看第 500~550 条 → 只给 `offset`/`limit`；
    - 在这一批结果里再找含某子串的 → 再加 `pattern`（大小写不敏感）。

    > 两者的成本**不一样**：纯翻页只解析取到的那一页（结果集是惰性物化的，
    > 见 `engine/index.py` 的 `_RowSource`）；而 `pattern` 要看每条的值才能
    > 过滤，必须把整个结果集解析一遍。所以在几万条的句柄上，
    > 纯翻页是毫秒级，带 `pattern` 会明显更慢 —— 缩小范围优先用
    > `search_*` 的参数（`pattern` / `subtype_of` / `owner` / `assembly`）。

    > 原先另有一个 `search_query_result`，实测它在 `pattern=""` 时与本工具
    > 输出完全相同 —— 它就是本工具的特例。按契约第 3 条（单一职责：
    > 一个是另一个的特例就合并）已删除，并入 `pattern` 参数。

    > **句柄有两个来源，本工具都能读**：`search_*` 这类静态查询登记的索引句柄，
    > 以及响应超限被截断时登记的 `cap-` 句柄（那种句柄可能在没有索引时也存在）。
    > 句柄只在**它所属的那一个会话**内有效；过期时返回 `available_handles`
    > 列出当前还能用的，绝不静默换成另一个结果集。

    Args:
        handle:  search_classes 等返回的 handle。
        offset:  起始位置。
        limit:   本页条数（上限 2000）。
        pattern: 在结果集内二次过滤的子串；留空表示不过滤（纯翻页）。
    """
    # 句柄有两个来源，都要能读（P2-4）：
    #   1. 索引句柄表 —— `search_*` 这类静态查询登记的；
    #   2. 会话级 cap 句柄表 —— 响应超限被截断时登记的完整列表。
    # 第 2 种可能在**没有索引**时也存在（纯运行时工具返回的大列表），
    # 所以这里不能一上来就 `_need_index()` 报错，否则那种句柄永远读不出来。
    st = state.state()
    idx = st.index
    res: dict | None = None
    if idx is not None:
        try:
            res = idx.read_query_result(handle, offset=offset, limit=limit,
                                        pattern=pattern)
        except Exception as exc:
            return _err(f"读取失败：{exc}")
        if res and res.get("found", True):
            out = _ok(**res)
            if pattern and not (res.get("items") or []):
                # 过滤零命中必须说清是「过滤没中」还是「句柄本身是空的」
                out["note"] = (f"句柄内共 {res.get('total', 0)} 条，但过滤条件 "
                               f"{pattern!r} 没有命中（或该页已越界）；"
                               f"去掉 pattern 可看全部。")
            return out

    qr = st.cap_results.get(handle)
    if qr is not None:
        out = _ok(found=True, **qr.page(offset, limit, pattern=pattern))
        if pattern and not (out.get("items") or []):
            out["note"] = (f"句柄内共 {out.get('total', 0)} 条，但过滤条件 "
                           f"{pattern!r} 没有命中（或该页已越界）；"
                           f"去掉 pattern 可看全部。")
        return out

    # 两个表都没有：如实报错，并把**两边**能用的句柄都给出来
    available = list((res or {}).get("available_handles") or [])
    available += [h for h in st.cap_results.handles() if h not in available]
    prefixes = []
    if idx is not None:
        prefixes.append(idx.results.prefix)
    prefixes.append(st.cap_results.prefix)
    return _err(
        f"句柄不存在或已过期：{handle}",
        available_handles=available,
        hint=("句柄只在**它所属的那一个会话**内有效：句柄前缀带引擎与代次"
              f"（当前会话前缀 {'、'.join(prefixes)}）。切换游戏或重建索引后"
              "旧句柄一律失效 —— 这是刻意的：否则旧句柄会静默读到"
              "另一个结果集（实测过，见 index.py 模块上方说明）"),
    )


# ================================================================
# 5. 运行时层
# ================================================================


@mcp.tool(annotations=_ann("注入游戏进程", read_only=False, destroy=False, idempotent=True, open_world=True))
@_cap_response
def inject_process(pid: int = 0, auto_find: bool = True,
                   preferred_module: str = "") -> dict:
    """把运行时探针注入正在运行的游戏进程。目标换了游戏时会**把会话身份与索引一起换过去**，并在返回里给 `context_switched`。

    注入后才能使用 runtime_* / hook_* / drain_events 等在线工具。
    同一个进程只会注入一次（重复调用会复用，不会重复 Hook）。

    **会话上下文会自动跟随目标**：注入的进程若属于另一个游戏，会话身份
    （game_dir / engine / metadata_path）与静态索引会一起切过去，返回里给
    `context_switched` 说明换了哪个游戏。为什么必须切：否则 `search_*`（旧游戏）
    与 `runtime_*`（新游戏）属于两个游戏，token 对账这把「尺子」本身就是错的。
    反推不出目标属于哪个游戏时**不猜**，改用 `context_warning` 如实提示。

    权限：**不需要管理员**。实测（2026-10）普通用户权限注入 IL2CPP 游戏
    成功（本进程与游戏进程的完整性级别都是 `Medium`，attach 后取运行时、
    枚举实例都正常）。唯一要匹配的是**权限级**：游戏若以管理员身份运行
    （完整性级别 `High`），普通权限的 Agent 才注入不进去。
    游戏带反调试 / 反注入时也会失败 —— 那是保护机制，不是权限问题。

    Args:
        pid:             目标进程号。留空则自动找当前游戏的主程序。
        auto_find:       没给 pid 时是否自动查找。
        preferred_module: IL2CPP 模块名，默认自动识别
                          （GameAssembly.dll / libil2cpp.so）。

    Returns:
        {ok, pid, init: {module, base, api_available, api_missing, fallbacks},
         context_switched?, context_warning?}
    """
    res = ensure_runtime(pid=pid, auto_find=auto_find,
                         preferred_module=preferred_module)
    if not res.get("ok"):
        return _err(res.get("error", "注入失败"), **{
            k: v for k, v in res.items() if k not in ("ok", "error")
        })
    # 把 pid 提到顶层，Agent 拿到结果就能直接用，不必再翻 runtime 子对象。
    # 注意：res 里本身可能带 pid 键，直接 _ok(pid=..., **res) 会
    # TypeError: got multiple values for keyword argument 'pid'，
    # 所以先把它摘掉再合并。
    rt = res.get("runtime") or {}
    body = {k: v for k, v in res.items() if k not in ("ok", "pid")}
    out = _ok(pid=rt.get("pid") or pid, **body)
    # 上下文不对劲时必须说出来：注入的目标与「当前选中的游戏」可能不是同一个，
    # 此时 search_* 与 runtime_* 的结果互相不能印证（详见 align_context_with_exe）。
    # 两块提示都由 state 侧算好（它才知道 exe 与反推结果），这里只负责带出来 ——
    # 与 launch_game 保持同一套字段名，免得两个工具说法不一致。
    align = res.get("align") or {}
    if align.get("switched") and "context_switched" not in out:
        out["context_switched"] = (
            f"会话上下文已从 {align.get('previous_game_dir') or '(空)'} "
            f"切换到 {align.get('game_dir')}"
            f"（引擎 {align.get('engine')}）—— 后续 search_* / class_detail "
            "查的是这个新游戏。")
    text = align.get("context_warning")
    if text and "context_warning" not in out:
        out["context_warning"] = text
    return out


@mcp.tool(annotations=_ann("启动并注入游戏", read_only=False, destroy=False, open_world=True))
@_cap_response
def launch_game(exe: str = "", args: str = "", preferred_module: str = "") -> dict:
    """启动游戏并**在游戏初始化之前**注入探针（**能抢在初始化前挂钩**）。

    **同名进程已在跑时直接拒绝**（避免白等 60 秒再报「IL2CPP 未就绪」）；
    `exe` 指向另一个游戏时会自动切换会话上下文（建新索引），
    返回里给 `context_switched` 说明换了哪个。

    比先手动开游戏再 inject_process 更强：能在对象被创建之前就装好 Hook，
    适合拦截只执行一次的初始化逻辑。

    **会话上下文会自动跟随 `exe`**：若它属于另一个游戏，会话身份与静态索引
    会一起切过去（并建索引），返回里给 `context_switched` 说明。
    否则 `search_*` 与 `runtime_*` 会分属两个游戏（实测过：index 140 个 image
    对上运行时 143 个，`image_count_matches=false`，而工具全程 ok:true）。
    反推不出 exe 所属游戏时**不猜**，用 `context_warning` 如实提示。

    Args:
        exe:  游戏可执行文件路径。留空则用当前会话识别出的主程序。
        args: 附加命令行参数。按 Windows 习惯拆：空白分隔，双引号内保留空白
              （所以含空格的路径要写成 `-path "C:\\Games\\My Game\\a.txt"`）。
              反斜杠**不是**转义符，Windows 路径可以直接写。
    """
    target = exe
    if not target:
        st = state.state()
        target = st.exe
        if not target and st.game_dir:
            target = discovery.pick_main_exe(st.game_dir)
    if not target or not Path(target).is_file():
        return _err(f"可执行文件不存在：{target or '(未指定)'}",
                    hint="先用 open_game 或 inspect_game 确认主程序路径")

    # 同名进程已经在跑？**必须**在这里拦住，不能放它去白等超时。
    # 原因：Unity 游戏普遍带单实例锁，第二个进程不会加载 GameAssembly.dll，
    # 于是 `ensure_runtime(spawn=True)` 会轮询 60 秒后报「IL2CPP 运行时未就绪」——
    # 把 Agent 引向「探针坏了」的错误方向。实测踩过：探针脚本异常残留了一个
    # 旧进程，之后每次 launch_game 都是 60 秒超时 + 这个误导性错误。
    if not _has_live_session():
        running = _same_named_processes(target)
        if running:
            pid0 = running[0]["pid"]
            return _err(
                f"已经有同名进程在运行：{Path(target).name}（pid={pid0}）。"
                "Unity 游戏通常带单实例锁，新进程不会加载 GameAssembly.dll，"
                "继续启动只会白等到超时。",
                hint=f"二选一：① 直接注入已在跑的那个进程："
                     f"inject_process(pid={pid0})；"
                     f"② 先结束它：taskkill /F /PID {pid0}，再 launch_game。",
                running_pid=pid0, target=target)

    argv = _split_argv(args) if args else []
    res = ensure_runtime(spawn=True, launcher_exe=target,
                         preferred_module=preferred_module)
    if not res.get("ok"):
        # 把下层的 hint 一起透传：例如「已有会话，先 close_session」
        # 这类出路信息比单纯的 error 更有用，丢掉会让 Agent 无从下手。
        extra = {k: v for k, v in res.items() if k not in ("ok", "error")}
        return _err(res.get("error", "启动失败"), target=target, argv=argv,
                    **extra)
    rt = res.get("runtime") or {}
    # 同 inject_process：res 里可能已有 pid/target/argv 键，先摘掉避免重复关键字
    body = {k: v for k, v in res.items()
            if k not in ("ok", "pid", "target", "argv")}
    # 会话上下文跟随目标切换过，就必须说出来 —— 悄悄换掉「当前游戏是谁」
    # 会让后续 search_* 的结果看起来突然变了，而调用方不知道为什么。
    # （对齐由 ensure_runtime 在 spawn 成功后、wait_ready 之前完成：
    #  放在这里做会在「已有会话 → 拒绝 spawn」这条路上留下副作用。）
    align = res.get("align") or {}
    if align.get("switched"):
        body["context_switched"] = (
            f"会话上下文已从 {align.get('previous_game_dir') or '(空)'} "
            f"切换到 {align.get('game_dir')}"
            f"（引擎 {align.get('engine')}）—— 后续 search_* / class_detail "
            "查的是这个新游戏。")
    elif align.get("index_error"):
        body["context_warning"] = (
            "会话上下文已对齐，但索引未建成：" + str(align.get("index_error")))
    return _ok(target=target, argv=argv, pid=rt.get("pid"), **body)


@mcp.tool(annotations=_ann("查看运行时概况", read_only=True, open_world=True))
@_cap_response
def runtime_info() -> dict:
    """查看已注入游戏的**探针状态**：IL2CPP 模块、映像数量、可用 API。含静态/运行时一致性自检（`image_count_matches`）。

    **一致性自检**：`image_count` 应与静态索引里的程序集数量吻合；
    不吻合说明探针挂到的可能不是同一个游戏实例，需要排查。
    """
    st = state.state()
    if not st.has_runtime():
        return _err("当前没有已注入的运行时会话。请先调用 inject_process "
                    "或 launch_game。")
    try:
        ri = st.runtime.runtime_info()
    except Exception as exc:
        return _err(f"查询失败：{exc}")
    out = dict(ri)
    # 附上静态侧的程序集数，方便直接比对
    if st.has_index():
        try:
            n = st.index.conn.execute(
                "SELECT COUNT(DISTINCT image) FROM types").fetchone()[0]
            out["static_image_count"] = n
            out["image_count_matches"] = (ri.get("image_count") == n)
        except Exception:
            pass
    return _ok(**out)


@mcp.tool(annotations=_ann("查询类的运行时信息", read_only=True, open_world=True))
@_cap_response
def runtime_class_info(
    name: str,
    namespace: str = "",
    image: str = "",
    fields: bool = True,
    methods: bool = True,
    method_filter: str = "",
) -> dict:
    """查类的**运行时**实况：**真实字段偏移**与**方法真实地址**（RVA）。

    嵌套类型请传**完整嵌套名**（`search_classes` 的 `nested_name`）；
    短名命中多个时**拒绝并给 `candidates`**，不替你猜。

    与静态 class_detail 的区别（很重要）：
        · 静态元数据里的字段**没有偏移**（偏移是运行时泛型实例化后才定的）
        · 静态元数据里的方法只有 token，**没有地址**
        · 这里给出的是运行时真实值 —— 这才是能直接用来 Hook 的落点

    **嵌套类型请传完整嵌套名**：IL2CPP 的 namespace 只属于最外层类型，
    嵌套类型（状态机 State、协程体 <Xxx>d__0、私有内部类）的命名空间是空的、
    只有短名。实测 IL2CPP 样本 20,072 个类型里 7,056 个（35%）是嵌套类型，
    名为 State 的类型有 131 个而只有 1 个是顶层。所以查嵌套类型时 `name`
    要写成完整嵌套名（如 "TeamTapas.ProjectR.UI2.RUI_FloatingUI.State"，
    从 search_classes 的 `nested_name` 直接抄）。只传短名而命中多个时，
    本工具返回 `ok: false` + `ambiguous: true` + `candidates`，不会替你猜。

    Args:
        name:          类名（短名；嵌套类型请传完整嵌套名）。
        namespace:     命名空间（可选）。**不给也能用**：会按短名在**顶层类型**
                       里解析，唯一才采用；命中多个则返回 `ambiguous: true` +
                       `candidates`，让你自己指定。嵌套类型必须用完整嵌套名
                       （`candidates[].full_name`）—— 只用短名会被拒绝，
                       不会随便挑一个（短名会撞上同名的嵌套枚举/状态机）。
        image:         程序集名（可选，如 "TeamTapas.ProjectR.dll"）。
        method_filter: 只看名字含该子串的方法（类方法很多时用）。

    Returns:
        {ok, full_name, fields: [{name, type, offset, is_static}],
         methods: [{name, token, pointer, rva_hex, in_module, param_count}]}
    """
    st = state.state()
    if not st.has_runtime():
        return _err("当前没有已注入的运行时会话。请先调用 inject_process "
                    "或 launch_game。")
    try:
        res = st.runtime.class_info(
            namespace=namespace, name=name, image=image,
            fields=fields, methods=methods, method_filter=method_filter,
        )
        return res if res.get("ok") else _err(res.get("error", "查询失败"), **{
            k: v for k, v in res.items() if k not in ("ok", "error")
        })
    except Exception as exc:
        return _err(f"查询失败：{exc}")


@mcp.tool(annotations=_ann("枚举类的活对象实例", read_only=True, open_world=True))
@_cap_response
def find_instances(
    name: str,
    namespace: str = "",
    image: str = "",
    max_count: int = 100,
) -> dict:
    """**枚举类的活对象实例**（拿 `instance` 指针的正路 —— 此前只能靠 Hook 碰运气）。走 GC liveness；**IL2CPP 可用（实测毫秒级）**、Mono 受官方限制会如实报 `unsupported`（**不用空列表冒充「没有实例」**），值类型无堆实例则明确报错。

    为什么需要它：`read_field_value` / `set_field_value` / `hook_method`
    都要一个对象指针，而在此之前**唯一的办法**是「Hook 一个恰好会收到
    该对象的函数，从 `args[0]` 里捡」—— 那是碰运气。本工具直接问运行时
    「这个类现在有哪些实例」。

    实现走 **GC liveness**（Unity 自家内存分析器用的那套导出）。
    注意：`il2cpp_class_get_instances` 这种「按类直接列实例」的导出
    **并不存在**（5 个真实样本实测），别照着那个名字找。

    引擎差异（**都是如实声明，不是缺陷**）：

    | 引擎 | 情况 |
    |---|---|
    | IL2CPP | ✅ 可用，实测毫秒级（GameObject/Transform/String 各约 10ms） |
    | Mono | ⚠️ 官方头文件写明堆遍历「只在 pre-stop-world 回调内有效」，从外部线程调用**不保证**可用。拿不到时本工具返回 `ok:false` + `unsupported:true`，**不会**用空列表冒充「没有实例」 |

    两个明确的限制：

    - **值类型（struct）没有堆实例**，会直接报错说明，而不是返回空列表。
    - 实例数超过 `max_count` 时截断，`overflow:true` 表示「还有更多」。

    ⚠️ **返回的实例包含派生类**（这是最容易踩的坑）：
    `find_instances(name="Transform", namespace="UnityEngine")` 拿回来的
    实测**全是 `RectTransform`** —— 因为 Unity 里 UI 节点都是 RectTransform，
    而它们是 Transform 的派生类。这不是缺陷（liveness 语义如此），
    但**用基类的字段偏移去读派生类实例可能出错**。

    所以返回里带 `instances: [{pointer, class_name}]`
    与 `exact_class_count` / `derived_count` / `class_name_counts`，
    请按 `class_name` 甄别你要的是哪一种。

    ⚠️ 拿到的指针**只在游戏继续运行时有意义**：对象可能随时被 GC 回收，
    或随场景切换失效。应当**立即使用**（读字段 / 写字段 / 下 Hook），
    不要跨多个步骤长期保存。

    Args:
        name:      类名（嵌套类型传完整嵌套名，同 `runtime_class_info`）。
        namespace: 命名空间。
        image:     程序集名。
        max_count: 最多返回多少个实例（默认 100，上限 100000）。

    Returns:
        {ok, class, engine, count, pointers: [...],
         instances: [{pointer, class_name}],
         exact_class_count, derived_count, class_name_counts, overflow}
        也可能带 `unsupported`（引擎不支持这条路径，如 Mono 的时机限制）
        或 `warning`（恢复 GC world 失败，游戏可能卡住）。
    """
    st = state.state()
    if not st.has_runtime():
        return _err("当前没有已注入的运行时会话。请先调用 inject_process "
                    "或 launch_game。")
    try:
        res = st.runtime.find_instances(
            namespace=namespace, name=name, image=image,
            max_count=max_count,
        )
        if res.get("ok"):
            return res
        # 失败路径：**把探针的 detail 摊平到顶层**。
        # 探针的 `fail(msg, extra)` 把附加信息放在 `detail` 里（全项目统一），
        # 但 `unsupported` 这类标记是调用方**直接读**的语义位，藏在
        # detail 里会被漏看 —— 实测：Mono 的时机限制返回了
        # `detail.unsupported=true`，而验收脚本读顶层读到 None，
        # 于是把「引擎限制」误判成「工具没按契约标注」。
        extra = {k: v for k, v in res.items() if k not in ("ok", "error")}
        detail = extra.pop("detail", None)
        if isinstance(detail, dict):
            for k, v in detail.items():
                extra.setdefault(k, v)
        return _err(res.get("error", "枚举实例失败"), **extra)
    except Exception as exc:
        return _err(f"枚举实例失败：{exc}")


@mcp.tool(annotations=_ann("定位方法的 Hook 落点", read_only=True, open_world=True))
@_cap_response
def find_hook_target(name: str, method: str, namespace: str = "",
                     image: str = "") -> dict:
    """定位某个方法可 Hook 的地址（含 RVA 与 token）。同样支持完整嵌套名，歧义时拒绝。

    这是「从静态元数据到 Hook 落点」的关键一步：
    先用 search_classes/class_detail 从元数据确认目标方法（拿到 token），
    再用本工具在运行时把类实体化，拿到真实地址；
    最后用返回的 token 与静态 token 比对，确认挂的是同一个方法。

    返回的每个 candidate 里 `address` 可直接喂给 hook_address；
    `rva_hex` 可与静态元数据的 RVA 对照，`token` 用于确认是同一个重载。

    Args:
        name:      类名（短名即可；嵌套类型传完整嵌套名）。
        method:    方法名（重载时看 candidates 的 param_count 区分）。
        namespace: 命名空间（可选）。不给时按短名在**顶层类型**里解析，
                   唯一才采用，多个则返回 `ambiguous: true` + `candidates`。
        image:     程序集名。
    """
    st = state.state()
    if not st.has_runtime():
        return _err("当前没有已注入的运行时会话。请先调用 inject_process "
                    "或 launch_game。")
    try:
        res = st.runtime.method_target(namespace=namespace, name=name,
                                       method=method, image=image)
        if not res.get("ok"):
            return _err(res.get("error", "未找到"), **{
                k: v for k, v in res.items() if k not in ("ok", "error")
            })
        # 探针内部叫 pointer，而 hook_address 的入参叫 address。
        # 这里是两套命名唯一会碰面的地方，统一补一个 address 别名，
        # 免得 Agent 拿到 pointer 却不知道要传给 hook_address 的哪个参数。
        for cand in res.get("candidates") or []:
            if cand.get("pointer") and not cand.get("address"):
                cand["address"] = cand["pointer"]
        return _ok(**res)
    except Exception as exc:
        return _err(f"定位失败：{exc}")


@mcp.tool(annotations=_ann("Hook 方法", read_only=False, destroy=False, open_world=True))
@_cap_response
def hook_method(
    name: str,
    method: str,
    namespace: str = "",
    image: str = "",
    args_type: str = "",
    label: str = "",
    max_calls: int = 200,
    backtrace: bool = False,
    token: str = "",
    param_count: int = -1,
    retval_type: str = "",
) -> dict:
    """在目标方法上安装 Hook，捕获它的调用（参数、调用来源、返回值）。用 `token` 指定重载；多候选未指定则**拒绝**。

    **风险**：Hook 会改变游戏运行行为，且错误的参数类型可能崩溃游戏。
    建议先用 find_hook_target 确认落点与 token。

    **重载**：同名方法可能有多个重载，token 各不相同。若目标方法有多个
    重载而你又没给 `token` / `param_count`，本工具会**拒绝猜测**并返回
    候选清单（`ok: false`）。这是故意的：盲挂到错误的重载上，
    Hook 照样会成功，错误要到很久以后才以「数据不对」的形式暴露。
    最可靠的做法是从静态元数据取目标重载的 token 传进来。

    Args:
        name:      类名。
        method:    方法名。
        namespace: 命名空间。
        image:     程序集名。
        args_type: 参数类型列表，逗号分隔，用于解析参数值。
                   可选：int/uint/long/ulong/bool/float/double/ptr/string/cstring
                   例："ptr,int,float"。留空则不解析参数值。
                   写错规格名会直接报错（不再静默按指针读）。
                   **实例方法的第一个整型位置是 `this`**：想拿对象指针就写
                   "ptr"（0 参数方法也一样，第 1 个 ptr 即 this）。
        label:     自定义标签，便于在事件里识别（默认 "类名.方法名"）。
        max_calls: 最多记录多少次调用（防止高频方法刷爆事件队列）。
        backtrace: 是否记录调用栈（较慢，排障时用）。
        token:     目标重载的 token（如 "0x06000A3D"）。**选重载首选这个**，
                   静态元数据的 class_detail / get_method_source 里就有。
        param_count: 目标重载的参数个数（-1 表示不指定）。仅在拿不到 token
                   且各重载参数个数不同时可用。
        retval_type: 返回值读取类型（int/uint/long/ulong/bool/float/double/
                   ptr/string/cstring）。留空 = 按静态返回类型自动推断，
                   这是推荐用法：float/double 的返回值在 XMM0 而不在 RAX，
                   按 ptr 读会给出毫无意义的整数。

    Returns:
        {ok, hook_id, class, method, chosen: {pointer, rva_hex, token},
         retval_type, return_type, candidate_count, usable_count}
        失败时 {ok: false, error, candidates: [...]}。
    """
    st = state.state()
    if not st.has_runtime():
        return _err("当前没有已注入的运行时会话。请先调用 inject_process "
                    "或 launch_game。")
    try:
        specs = _parse_arg_specs(args_type)
        rv = _parse_retval_type(retval_type)
    except ValueError as exc:
        return _err(str(exc))
    try:
        res = st.runtime.hook_method(
            namespace=namespace, name=name, method=method, image=image,
            args=specs, label=label, max_calls=max_calls, backtrace=backtrace,
            token=token, param_count=param_count, retval_type=rv,
        )
        return res if res.get("ok") else _err(res.get("error", "Hook 失败"), **{
            k: v for k, v in res.items() if k not in ("ok", "error")
        })
    except Exception as exc:
        return _err(f"Hook 失败：{exc}")


@mcp.tool(annotations=_ann("在指定地址安装 Hook", read_only=False, destroy=False, open_world=True))
@_cap_response
def hook_address(
    address: str,
    args_type: str = "",
    label: str = "",
    max_calls: int = 200,
    retval_type: str = "ptr",
    backtrace: bool = False,
) -> dict:
    """在**任意地址**安装 Hook（不限于托管方法）。适用于原生函数、系统 API、以及「模块基址 + RVA」算出的地址。

    适用于：原生游戏函数、系统 API、以及从静态分析里算出的地址
    （模块基址 + RVA）。

    Args:
        address:    目标地址，十六进制（如 "0x7ffa05f7f050"）。
        args_type:  参数类型列表，逗号分隔（同 hook_method）。
                    写错规格名会直接报错。
        retval_type: 返回值类型（int/uint/float/double/ptr/string/bool）。
        max_calls:  最多记录多少次。
        backtrace:  是否记录调用栈。
    """
    st = state.state()
    if not st.has_runtime():
        return _err("当前没有已注入的运行时会话。请先调用 inject_process "
                    "或 launch_game。")
    try:
        specs = _parse_arg_specs(args_type)
        rv = _parse_retval_type(retval_type) or "ptr"
    except ValueError as exc:
        return _err(str(exc))
    try:
        res = st.runtime.hook(address=address, args=specs, label=label,
                              max_calls=max_calls, backtrace=backtrace,
                              retval_type=rv)
        if not res.get("ok"):
            return _err(res.get("error", "Hook 失败"), **{
                k: v for k, v in res.items() if k not in ("ok", "error")
            })
        # 探针把 RVA 信息放在 target_rva 子对象里（address/rva/rva_hex/module）。
        # 与 find_hook_target 保持一致，提到顶层，便于和静态元数据的 RVA 对照。
        tr = res.get("target_rva") or {}
        for k in ("module", "rva", "rva_hex"):
            if k in tr and k not in res:
                res[k] = tr[k]
        res.setdefault("address", res.get("target"))
        return _ok(**res)
    except Exception as exc:
        return _err(f"Hook 失败：{exc}")


@mcp.tool(annotations=_ann("卸载 Hook", read_only=False, destroy=True, idempotent=True, open_world=True))
@_cap_response
def unhook(hook_id: str = "") -> dict:
    """卸载 Hook。hook_id 留空则卸载全部。

    卸载后游戏行为恢复原状。收尾时务必调用（或用 close_session）。
    """
    st = state.state()
    if not st.has_runtime():
        return _err("当前没有已注入的运行时会话。")
    try:
        if hook_id:
            res = st.runtime.unhook(hook_id)
        else:
            res = st.runtime.unhook_all()
        return res if res.get("ok") else _err(res.get("error", "卸载失败"), **{
            k: v for k, v in res.items() if k not in ("ok", "error")
        })
    except Exception as exc:
        return _err(f"卸载失败：{exc}")


@mcp.tool(annotations=_ann("查看 Hook 状态", read_only=True, open_world=True))
@_cap_response
def hook_status() -> dict:
    """列出当前所有 Hook 及其触发次数。

    触发次数可以用来判断「Hook 装对了没」：
    如果挂的是每帧调用的方法却始终 0 次，多半是落点不对。
    """
    st = state.state()
    if not st.has_runtime():
        return _err("当前没有已注入的运行时会话。")
    try:
        return _ok(**st.runtime.hook_status())
    except Exception as exc:
        return _err(f"查询失败：{exc}")


@mcp.tool(annotations=_ann("读取 Hook 事件", read_only=False, destroy=True, open_world=True))
@_cap_response
def drain_events(limit: int = 200, kind: str = "") -> dict:
    """取走探针缓冲的 Hook 事件（调用记录）。`kind` 过滤作用于**整条队列**（不是只看队首）。

    不匹配的事件会原序留在队列里，**不会被吞**；超出 `limit` 的匹配事件同样留下。

    语义与 AiMod 的 /v1/events?after= 一致：**取走即消费**。
    想持续观察就每隔一会儿调用一次，累加统计即可。

    `kind` 过滤的是**整条队列**，不是只看队首那一条 —— 注入后队首恒有一条
    `notice`(probe_ready)，若只在队首判断，`kind="hook"` 会一条都取不到
    （返回 `ok:true` + 空列表，调用方会误判成「Hook 没触发」而去排查落点）。
    不匹配的事件按原序留在队列里，不会被吞；超出 `limit` 的匹配事件同样留下。

    Args:
        limit: 本次最多取多少条。
        kind:  只取某类事件（如 "hook" / "notice" / "log"）。

    Returns:
        {ok, returned, pending_approx, dropped, events: [...]}
        事件形如 {type, id, label, phase: enter/leave, seq, thread,
                  from, args, retval}
    """
    st = state.state()
    if not st.has_runtime():
        return _err("当前没有已注入的运行时会话。")
    try:
        return _ok(**st.runtime.drain_events(limit=limit, kind=kind))
    except Exception as exc:
        return _err(f"读取失败：{exc}")


@mcp.tool(annotations=_ann("读游戏内存", read_only=True, open_world=True))
@_cap_response
def read_memory(address: str, size: int = 16) -> dict:
    """读取游戏进程内存（十六进制返回）。

    用于：确认字段偏移对不对、读对象原始字节、观察内存变化。

    Args:
        address: 地址（十六进制，如 "0x1a2b3c4d"）。
        size:    读取字节数（上限 16 MB）。
    """
    st = state.state()
    if not st.has_runtime():
        return _err("当前没有已注入的运行时会话。")
    try:
        res = st.runtime.read_memory(address, size)
        return res if res.get("ok") else _err(res.get("error", "读取失败"), **{
            k: v for k, v in res.items() if k not in ("ok", "error")
        })
    except Exception as exc:
        return _err(f"读取失败：{exc}")


@mcp.tool(annotations=_ann("写游戏内存", read_only=False, destroy=True, idempotent=True, open_world=True))
@_cap_response
def write_memory(address: str, hex_data: str, force: bool = False) -> dict:
    """写游戏进程内存。

    ⚠️ **危险操作**：写错地址会立刻崩溃游戏。
    写之前务必先用 read_memory 确认地址内容符合预期。
    建议只改浮点数/整数这类简单值，并先备份原值。

    **不要写已装 Hook 的函数入口**：Frida 的 Interceptor 拥有那几字节
    （它在那里写了跳转指令），绕过它直接 `Memory.protect` + 写同一块内存，
    **哪怕写回刚读到的同样字节**，也会在数秒内让游戏崩溃 ——
    实测两次：Windows 事件日志里是 `GameAssembly.dll` 在该方法 RVA 处的
    `0xc0000005`；同样操作写在模块头（`base`）上则完全无害且能原样读回。
    本工具默认**拒绝**这种写入；确实要写请传 `force=True`，
    或者先 `unhook` 再写。

    Args:
        address:  地址（十六进制）。
        hex_data: 要写入的字节，十六进制串（如 "0000803f" 表示 float 1.0）。
        force:    明知目标被 Hook 覆盖仍要写（默认 False = 拒绝）。
    """
    st = state.state()
    if not st.has_runtime():
        return _err("当前没有已注入的运行时会话。")
    size = len(hex_data.strip()) // 2
    if not force:
        hit = _overlapping_hook(st, address, size)
        if hit:
            return _err(
                "拒绝写入：目标与已装 Hook 的函数入口重叠"
                f"（{hit['hook_id']} {hit.get('label')} @ {hit['target']}）。"
                "Frida 的 Interceptor 拥有那几字节，直接写会让游戏在数秒内崩溃"
                "（实测 0xc0000005）。要改行为请改数据字段，或先 unhook；"
                "确实要写请传 force=True。",
                hook=hit,
            )
    try:
        res = st.runtime.write_memory(address, hex_data)
        return res if res.get("ok") else _err(res.get("error", "写入失败"), **{
            k: v for k, v in res.items() if k not in ("ok", "error")
        })
    except Exception as exc:
        return _err(f"写入失败：{exc}")


@mcp.tool(annotations=_ann("读实例字段的值", read_only=True, open_world=True))
@_cap_response
def read_field_value(
    instance: str,
    name: str,
    field: str,
    namespace: str = "",
    image: str = "",
) -> dict:
    """读取某个对象实例上某个字段的原始值。**读几个字节由字段类型决定**（bool 1 / int 4 / long·double·引用 8）。

    自动完成三步：运行时查字段偏移 → 算出字段地址 → 读内存。

    **读几个字节由字段类型决定**（`_field_read_width`）：bool 读 1、int 读 4、
    long/double/引用类型读 8。原来固定读 8 字节，4 字节字段会连带读到**相邻
    字段**的 4 个字节 —— 既给出无意义的 `as_int64`（两个字段拼出来的数），
    也会在字段贴近页尾时把一次本可成功的读取变成假失败。

    返回里 `read_width` 是实际读取的字节数，解释字段也按宽度给：
    1 → `as_int8`/`as_uint8`/`as_bool`；2 → `as_int16`/`as_uint16`；
    4 → `as_int32`/`as_uint32`/`as_float`；8 → 再给 `as_int64`/`as_double`/`as_pointer`。

    局限：只适合**基础类型字段**（int/float/bool/enum 等）。
    引用类型字段读到的是指针，要读内容需再解引用（用 read_memory）。

    Args:
        instance:  对象指针（十六进制）。可从 Hook 事件的 args[0] 里拿到。
        name:      类名。
        field:     字段名。
        namespace: 命名空间。
        image:     程序集名。
    """
    st = state.state()
    if not st.has_runtime():
        return _err("当前没有已注入的运行时会话。请先调用 inject_process。")
    try:
        ci = st.runtime.class_fields(namespace=namespace, name=name,
                                     image=image, instance_only=True)
        if not ci.get("ok"):
            return _err(ci.get("error", "未找到类"), **{
                k: v for k, v in ci.items() if k not in ("ok", "error")
            })
        hit = None
        for f in ci.get("fields") or []:
            if f.get("name") == field:
                hit = f
                break
        if hit is None:
            return _err(
                f"类 {name} 里没有实例字段 {field}",
                available=[f.get("name") for f in (ci.get("fields") or [])],
            )
        off = hit.get("offset")
        if off is None:
            return _err(f"字段 {field} 没有运行时偏移（可能是静态字段）")

        # 字段地址 = 实例地址 + 偏移
        try:
            inst_val = int(instance, 16)
        except ValueError:
            return _err(f"instance 不是合法的十六进制地址：{instance}")
        field_addr = inst_val + int(off)

        # 按字段类型决定读几个字节：只读这个字段自己的字节，
        # 不把相邻字段的内容一起读进来（见 _field_read_width 的说明）。
        read_width = _field_read_width(hit.get("type"))
        raw = st.runtime.read_memory(hex(field_addr), read_width)
        if not raw.get("ok"):
            return _err(raw.get("error", "读取内存失败"),
                        field_address=hex(field_addr),
                        read_width=read_width)

        hexs = raw.get("hex") or ""
        out: dict[str, Any] = {
            "class": name,
            "field": field,
            "instance": instance,
            "offset": off,
            "field_address": hex(field_addr),
            "raw_hex": hexs,
            "field_type": hit.get("type"),
            "read_width": read_width,
        }
        # 按**实际读到的宽度**给解释，不给这个宽度下没有意义的解释：
        # 4 字节字段的 as_int64 是「本字段 + 下一个字段」拼出来的数，读它必然误判。
        import struct as _struct
        if len(hexs) >= 2:
            b1 = bytes.fromhex(hexs[:2])
            out["as_int8"] = _struct.unpack("<b", b1)[0]
            out["as_uint8"] = _struct.unpack("<B", b1)[0]
            out["as_bool"] = b1[0] != 0
        if len(hexs) >= 4:
            b2 = bytes.fromhex(hexs[:4])
            out["as_int16"] = _struct.unpack("<h", b2)[0]
            out["as_uint16"] = _struct.unpack("<H", b2)[0]
        if len(hexs) >= 8:
            b4 = bytes.fromhex(hexs[:8])
            out["as_int32"] = _struct.unpack("<i", b4)[0]
            out["as_uint32"] = _struct.unpack("<I", b4)[0]
            out["as_float"] = round(_struct.unpack("<f", b4)[0], 6)
        if len(hexs) >= 16:
            b8 = bytes.fromhex(hexs[:16])
            out["as_int64"] = _struct.unpack("<q", b8)[0]
            out["as_double"] = _struct.unpack("<d", b8)[0]
            out["as_pointer"] = hex(_struct.unpack("<Q", b8)[0])
        return _ok(**out)
    except Exception as exc:
        return _err(f"读取字段失败：{exc}")


@mcp.tool(annotations=_ann("写入对象实例的字段值", read_only=False, open_world=True,
                           destroy=True, idempotent=True))
@_cap_response
def set_field_value(
    instance: str,
    name: str,
    field: str,
    value: Any,
    namespace: str = "",
    image: str = "",
    verify: bool = True,
) -> dict:
    """**写对象字段值**（`read_field_value` 的对称入口）：自动查偏移、按类型定宽度与编码、写完回读核对。**溢出直接报错不截断**；**引用类型字段拒绝**（改引用属改逻辑，请用 `eval_script`/Hook）。

    ⚠️ **危险操作**：写错地址或写错值会立刻让游戏崩溃或行为异常。
    写之前**务必先 `read_field_value` 读一次**，确认偏移与当前值符合预期。

    与 `write_memory` 的分工（不是重复工具）：
    `write_memory` 要「地址 + 裸十六进制字节」，是**原始内存**入口；
    本工具要「哪个对象的哪个字段 + 一个语义化的值」，是**字段**入口。
    它替你做了三件容易做错的事：查真实偏移、按字段类型定宽度、
    按字段类型选整数/浮点/布尔的编码。

    **写几个字节由字段类型决定**（与读取同一张表）：bool 1、int16 2、
    int32/float 4、long/double/引用类型 8。**溢出直接报错，不静默截断** ——
    把 300 写进 1 字节字段如果截成 44，你会看到 `ok:true` 却拿到没要的值。

    **引用类型字段（class/string/数组）会被拒绝**：`field_type` 认不出基础类型
    且宽度为 8 时，本工具报错而不是去写一个指针 —— 换对象引用属于改逻辑，
    应该用 `eval_script` 或 Hook，而不是改一个字段的字节。

    Args:
        instance:  对象指针（十六进制）。可用 `find_instances` 拿，
                   或从 Hook 事件的 args[0] 拿。
        name:      类名。
        field:     字段名。
        value:     要写入的值。整数按字段宽度与符号解释；浮点字段给数字；
                   bool 字段给 true/false/1/0；认不出类型时给 `0x...` 字节串。
        namespace: 命名空间。
        image:     程序集名。
        verify:    写完后立刻回读一次并放进返回（默认 True）。
                   **这是为了不留「假成功」**：写完把实测值给你，而不是只报 ok。

    Returns:
        {ok, class, field, instance, offset, field_address, wrote_hex,
         write_width, write_kind, value_before?, read_back?...}
    """
    st = state.state()
    if not st.has_runtime():
        return _err("当前没有已注入的运行时会话。请先调用 inject_process。")
    try:
        ci = st.runtime.class_fields(namespace=namespace, name=name,
                                     image=image, instance_only=True)
        if not ci.get("ok"):
            return _err(ci.get("error", "未找到类"), **{
                k: v for k, v in ci.items() if k not in ("ok", "error")
            })
        hit = None
        for f in ci.get("fields") or []:
            if f.get("name") == field:
                hit = f
                break
        if hit is None:
            return _err(
                f"类 {name} 里没有实例字段 {field}",
                available=[f.get("name") for f in (ci.get("fields") or [])],
            )
        if hit.get("is_static") is True:
            return _err(f"字段 {field} 是静态字段，本工具只写实例字段"
                        "（静态字段请用 eval_script 调 il2cpp_field_static_set_value）")

        off = hit.get("offset")
        if off is None:
            return _err(f"字段 {field} 没有运行时偏移")

        try:
            inst_val = int(instance, 16)
        except (ValueError, TypeError):
            return _err(f"instance 不是合法的十六进制地址：{instance}")

        ftype = hit.get("type")
        width = _field_read_width(ftype)
        kind = _field_write_kind(ftype, width)

        # 宽度 8 且类型认不出来 = 多半是引用类型。写指针会改逻辑，明确拒绝。
        if kind == "long" and (not ftype or str(ftype).strip() not in _FIELD_WRITE_KIND):
            return _err(
                f"字段 {field} 的类型 {ftype!r} 认不出（宽度 8），"
                f"疑似**引用类型**。写入指针属于改逻辑，请改用 eval_script / Hook；"
                f"确实要写裸字节请用 write_memory。",
                field_type=ftype, field_address=hex(inst_val + int(off)),
            )

        blob, enc_err = _encode_field_value(value, width, kind)
        if blob is None:
            return _err(f"值编码失败：{enc_err}",
                        field_type=ftype, write_width=width, write_kind=kind)

        field_addr = inst_val + int(off)

        out: dict[str, Any] = {
            "class": name,
            "field": field,
            "instance": instance,
            "offset": off,
            "field_address": hex(field_addr),
            "field_type": ftype,
            "write_width": width,
            "write_kind": kind,
            "wrote_hex": blob.hex(),
        }

        # 先读原值：既给回滚依据，也让「写前后有差别」这件事可核查
        before = st.runtime.read_memory(hex(field_addr), width)
        if before.get("ok"):
            out["value_before_hex"] = before.get("hex")
            out["value_before"] = _explain_bytes(before.get("hex") or "")

        wr = st.runtime.write_memory(hex(field_addr), blob.hex())
        if not wr.get("ok"):
            return _err(wr.get("error", "写入内存失败"),
                        field_address=hex(field_addr), wrote_hex=blob.hex())

        if verify:
            after = st.runtime.read_memory(hex(field_addr), width)
            if after.get("ok"):
                out["read_back_hex"] = after.get("hex")
                out["read_back"] = _explain_bytes(after.get("hex") or "")
                out["verified"] = (after.get("hex") or "").lower() == blob.hex().lower()
                if not out["verified"]:
                    # 写完不是自己写的内容 —— 必须说出来，不能报成功
                    return _err(
                        "写入后回读到的字节与写入值不一致（可能被游戏立刻覆盖，"
                        "或地址不对）。**不要**当成成功。",
                        **out,
                    )
            else:
                out["verified"] = None
                out["verify_error"] = after.get("error")
        return _ok(**out)
    except Exception as exc:
        return _err(f"写入字段失败：{exc}")


def _explain_bytes(hexs: str) -> dict:
    """按**实际长度**解释一段小端字节，只给这个长度下有意义的解释。

    与 `read_field_value` 用同一口径（那边是内联的），抽出来给
    `set_field_value` 复用，避免两处对同一段字节给出不同解释。
    """
    import struct as _struct
    out: dict[str, Any] = {}
    try:
        if len(hexs) >= 2:
            b1 = bytes.fromhex(hexs[:2])
            out["as_int8"] = _struct.unpack("<b", b1)[0]
            out["as_uint8"] = _struct.unpack("<B", b1)[0]
            out["as_bool"] = b1[0] != 0
        if len(hexs) >= 4:
            b2 = bytes.fromhex(hexs[:4])
            out["as_int16"] = _struct.unpack("<h", b2)[0]
            out["as_uint16"] = _struct.unpack("<H", b2)[0]
        if len(hexs) >= 8:
            b4 = bytes.fromhex(hexs[:8])
            out["as_int32"] = _struct.unpack("<i", b4)[0]
            out["as_uint32"] = _struct.unpack("<I", b4)[0]
            out["as_float"] = round(_struct.unpack("<f", b4)[0], 6)
        if len(hexs) >= 16:
            b8 = bytes.fromhex(hexs[:16])
            out["as_int64"] = _struct.unpack("<q", b8)[0]
            out["as_double"] = _struct.unpack("<d", b8)[0]
            out["as_pointer"] = hex(_struct.unpack("<Q", b8)[0])
    except (ValueError, struct.error):  # pragma: no cover - 防御
        pass
    return out

@mcp.tool(annotations=_ann("内存特征码扫描", read_only=True, open_world=True))
@_cap_response
def scan_pattern(pattern: str, module: str = "", max_results: int = 32) -> dict:
    """在游戏模块里做 AOB（特征码）扫描。**这是兜底手段** —— 优先用元数据 + RVA 定位。

    ⚠️ **这是兜底手段**。优先用元数据 + RVA 定位：
    本工具集的核心优势就是「不用扫内存也能找到 Hook 落点」。
    只有当符号被剥离、或目标是原生代码时，才用扫描。

    Args:
        pattern:     十六进制特征码，支持通配符 "??"。
                     例："48 8B ?? ?? E8"。
        module:      限定模块名（默认当前 IL2CPP 模块）。
        max_results: 最多返回多少个命中。
    """
    st = state.state()
    if not st.has_runtime():
        return _err("当前没有已注入的运行时会话。")
    try:
        res = st.runtime.scan_pattern(pattern, module=module,
                                      max_results=max_results)
        return res if res.get("ok") else _err(res.get("error", "扫描失败"), **{
            k: v for k, v in res.items() if k not in ("ok", "error")
        })
    except Exception as exc:
        return _err(f"扫描失败：{exc}")


@mcp.tool(annotations=_ann("在游戏内执行 JS", read_only=False, destroy=True, open_world=True))
@_cap_response
def eval_script(code: str) -> dict:
    """在游戏进程内执行**任意** Frida JS 代码 —— 应急逃生舱。

    与 AiMod 的 eval_script 同定位：当预置工具不够用时，
    直接下发 JS 探测。

    可用全局对象：`mod` / `api` / `funcs` / `hooks`（IL2CPP 侧），
    以及 `monoFuncs` 与 `BE`（Mono 侧的已绑定导出、以及当前选中的后端对象）。
    后者是排查 Mono 问题的必需品：例如
    `"typeof monoFuncs.mono_field_get_offset"` 能直接看出某个导出有没有绑上，
    不必去复刻绑定逻辑。

    **返回值必须是可 JSON 序列化的**（字符串/数字/布尔/数组/普通对象）。

    示例：
        "Process.arch"
        "mod.base.add(0x1000).toString()"
        "(function(){ return Process.enumerateModules().length; })()"
        "typeof monoFuncs.mono_get_root_domain"

    Args:
        code: JS 表达式或语句块。
    """
    st = state.state()
    if not st.has_runtime():
        return _err("当前没有已注入的运行时会话。")
    try:
        res = st.runtime.eval_js(code)
        return res if res.get("ok") else _err(res.get("error", "执行失败"), **{
            k: v for k, v in res.items() if k not in ("ok", "error")
        })
    except Exception as exc:
        return _err(f"执行失败：{exc}")


@mcp.tool(annotations=_ann("开关 GC", read_only=False, destroy=False, idempotent=True, open_world=True))
@_cap_response
def gc_control(disable: bool = True) -> dict:
    """暂停/恢复游戏的 GC（垃圾回收）。

    Hook 期间建议暂停 GC，避免正在观察的对象被回收掉。
    **观察结束后务必恢复（disable=False）**，否则游戏内存会持续增长。

    Args:
        disable: True 暂停 GC，False 恢复。
    """
    st = state.state()
    if not st.has_runtime():
        return _err("当前没有已注入的运行时会话。")
    try:
        res = st.runtime.gc_control(disable=disable)
        return res if res.get("ok") else _err(res.get("error", "GC 控制失败"))
    except Exception as exc:
        return _err(f"GC 控制失败：{exc}")


# ================================================================
# 6. 弱视觉 / UI 层（截图 · OCR · 日志）
# ================================================================
#
# 为什么叫「弱视觉」：调研了 7 个游戏自动化 Bot 项目后确认，游戏 Mod 的
# **验收**绝大多数时候不需要强视觉 ——
#   「找到开始游戏按钮」→ OCR 拿到文本与坐标
#   「确认 Mod 面板起来了」→ OCR 找标题，或 probe_log 找日志行
# 真正需要的只是**文本**与**点哪里**。
#
# 所以这一层的纪律是：
#   · 截图是**证据**，不是判据（podium-mcp 原话：Screenshots are NEVER an oracle）；
#   · 坐标一律**屏幕绝对坐标**，裁剪图不回传基准会让 Agent 点偏；
#   · 截图只落盘、不回 base64（本项目 256 KB 上限 vs 单图约 5 万 token）；
#   · 依赖全部**可选**，缺了如实报并与其它能力解耦。


@mcp.tool(annotations=_ann("截取屏幕或窗口（存盘，返回路径）",
                           read_only=False, open_world=True,
                           destroy=False, idempotent=True))
@_cap_response
def capture_screen(region: str = "", window: str = "", save_path: str = "",
                   max_side: int = 1920) -> dict:
    """截屏 / 截窗口**存盘并返回路径**（不回 base64）。**截图是证据、不是判据**；窗口找不到会如实失败，不静默改截整屏。

    ⚠️ **截图不是判据，只是证据**。要判断「现在在哪个界面」请用 `ocr`。
    这条规矩来自同类项目的教训（`podium-mcp` 源码原文：
    *"Screenshots are NEVER an oracle."*）—— 让模型看像素去下结论，
    既贵（单张截图约 5 万 token）又不可靠。

    **不返回 base64**：本项目任何返回都不超过 256 KB，而一张 2560x1440
    的 PNG base64 会直接把它撑爆。图落在磁盘上，返回路径。

    典型用途：验收失败时**留证据**（配合 `ocr` 的 `save_on_failure`）。

    Args:
        region:    "x,y,w,h" 屏幕绝对坐标；不传则截整个主屏。
        window:    窗口标题**子串**；给了就用该窗口的矩形（优先于 region）。
                   找不到会**如实失败**，不会静默退化成截整屏。
        save_path: 保存路径；不传则落临时目录（返回里给路径）。
        max_side:  证据图最长边上限（等比缩小，默认 1920）；0 = 不缩放。
                   **只影响存盘的证据图，不影响坐标语义。**

    Returns:
        {ok, path, width, height, backend, region, coordinate_space, ...}
    """
    try:
        from .engine import screen as _screen
    except Exception as exc:  # pragma: no cover
        return _err(f"加载截图模块失败：{exc}")

    reg = _parse_region(region)
    if reg is _BAD_REGION:
        return _err(f"region 格式不对：{region!r}，应为 \"x,y,w,h\"（四个整数）")
    try:
        res = _screen.grab(region=reg, window=window, save_path=save_path,
                           max_side=max(0, int(max_side)))
    except Exception as exc:
        return _err(f"截图失败：{exc}")
    if not res.get("ok"):
        return _err(res.get("error", "截图失败"),
                    **{k: v for k, v in res.items() if k not in ("ok", "error")})
    return _ok(**res)


@mcp.tool(annotations=_ann("OCR 识别屏幕文本并给屏幕绝对坐标",
                           read_only=False, open_world=True,
                           destroy=False, idempotent=True))
@_cap_response
def ocr(region: str = "", window: str = "", expected: str = "",
        match: str = "contains", threshold: float = 0.3,
        case_sensitive: bool = False, max_items: int = 200,
        save_on_failure: str = "") -> dict:
    """**OCR 识别文本并给屏幕绝对坐标**（可直接用于点击）。只对给定 ROI 做（实测整屏 4.1s vs ROI 数十 ms）。

    `matched_count==0` 是「**本次没找到**」而**非**「不存在」；
    `save_on_failure` 可在没匹配到时留下截图证据。

    这是「弱视觉」的主力：验收时绝大多数判断只需要
    「屏幕上有没有这几个字、它在哪」。

    **只对给定的区域做 OCR**（实测整屏 2560x1440 要 4.1 秒，而 ROI
    通常几十毫秒）—— 这也是同类项目的标配做法。

    坐标一律是**屏幕绝对坐标**，可直接喂给点击；`coordinate_space`
    字段会明说是 `screen_absolute`，避免「拿裁剪图坐标去点」这个经典坑。

    匹配 `expected` 时的语义（**重要**）：
      · `matched_count == 0` 的意思是「**本次没找到**」，
        **不等于**「游戏里没有」—— 可能界面还没加载、或文字被 OCR
        拆成了多个框。返回里的 `note` 会写明这一点。
      · `best_match` 取的是**置信度最高**的那条，不是第一条。

    Args:
        region:   "x,y,w,h" 屏幕绝对坐标；与 `window` 二选一。
        window:   窗口标题子串；用它定 ROI。
        expected: 期望出现的文本；给了就把命中的单独列出来。
        match:    `contains`（默认）/ `exact` / `regex`。
        threshold: 置信度下限，低于它的丢弃（默认 0.3）。
        case_sensitive: 文本匹配是否区分大小写。
        max_items: 最多返回多少个文本框。
        save_on_failure: 给了路径时，若没匹配到就**把当时的截图存到这里**
                   （等价 MaaFramework 的 `save_on_error`）—— 失败要留证据。

    Returns:
        {ok, engine, count, results: [{text, score, box, center, click_point}],
         texts, matched, matched_count, best_match, click_point,
         coordinate_space: "screen_absolute", image, ...}
    """
    try:
        from .engine import screen as _screen
    except Exception as exc:  # pragma: no cover
        return _err(f"加载 OCR 模块失败：{exc}")

    reg = _parse_region(region)
    if reg is _BAD_REGION:
        return _err(f"region 格式不对：{region!r}，应为 \"x,y,w,h\"（四个整数）")
    if match not in ("contains", "exact", "regex"):
        return _err(f"match 只能是 contains / exact / regex，收到 {match!r}")
    try:
        res = _screen.ocr(region=reg, window=window, expected=expected,
                          match=match, threshold=float(threshold),
                          case_sensitive=bool(case_sensitive),
                          max_items=max(1, int(max_items)),
                          save_on_failure=save_on_failure)
    except ValueError as exc:
        return _err(str(exc))
    except Exception as exc:
        return _err(f"OCR 失败：{exc}")
    if not res.get("ok"):
        return _err(res.get("error", "OCR 失败"),
                    **{k: v for k, v in res.items() if k not in ("ok", "error")})
    return _ok(**res)


@mcp.tool(annotations=_ann("读取游戏日志尾部并按正则过滤", read_only=True))
@_cap_response
def probe_log(path: str, pattern: str = "", max_lines: int = 200,
              tail_bytes: int = 262144) -> dict:
    """读游戏日志**尾部**并按正则过滤（编码自动 utf-8/gbk）。**只读、不消费**，可反复轮询。

    同类 Bot 项目普遍没有这条「用日志确认结果」的通道。

    **为什么单独做这件事**：调研的 7 个游戏自动化 Bot 项目里，
    **没有一个**用游戏日志判断结果 —— 它们靠视觉、注册表或 CDP。
    而「通过日志确认结果」恰恰是最可靠的验收通道之一
    （Mod 自己的输出、游戏自己的报错都在日志里），所以这里补上。

    做法刻意保守：
      · 只读**尾部** `tail_bytes`（默认 256 KB）—— 日志动辄几百 MB，
        **绝不**整个读进来；
      · 给了 `pattern` 就只返回匹配行，并额外给 `matched_count`，
        便于断言「没有出现 ERROR」这类**否定式**判据；
      · 编码自动探测（utf-8 → utf-8-sig → gbk）—— 中文游戏常见 GBK。

    ⚠️ 它是**只读**的，不消费日志（不像 `drain_events` 那样取走即没）。
    同一次读取可以反复做，所以适合轮询等待某个日志行出现。

    Args:
        path:      日志文件路径。
        pattern:   正则；只返回匹配行。留空返回尾部所有行。
        max_lines: 最多返回多少行（默认 200，取**最后** N 行）。
        tail_bytes: 从文件末尾读多少字节（默认 262144）。

    Returns:
        {ok, path, size, encoding, total_lines_in_tail, returned,
         lines: [{line_no, text}], matched_count?, has_more?, truncated_head}
    """
    try:
        from .engine import screen as _screen
    except Exception as exc:  # pragma: no cover
        return _err(f"加载日志模块失败：{exc}")
    if not path:
        return _err("必须给 path")
    try:
        res = _screen.read_log(path=path, pattern=pattern,
                               max_lines=max(1, int(max_lines)),
                               tail_bytes=max(1024, int(tail_bytes)))
    except Exception as exc:
        return _err(f"读取日志失败：{exc}")
    if not res.get("ok"):
        return _err(res.get("error", "读取日志失败"),
                    **{k: v for k, v in res.items() if k not in ("ok", "error")})
    return _ok(**res)


#: `region="x,y,w,h"` 解析失败时的哨兵（与「没给 region」的 None 区分开）
_BAD_REGION = object()


def _parse_region(region: str) -> Any:
    """解析 `"x,y,w,h"`。空串 → None（表示不限定区域）。"""
    if not region:
        return None
    parts = [p.strip() for p in str(region).split(",")]
    if len(parts) != 4:
        return _BAD_REGION
    try:
        vals = [int(float(p)) for p in parts]
    except ValueError:
        return _BAD_REGION
    if vals[2] <= 0 or vals[3] <= 0:
        return _BAD_REGION
    return vals


def _eval_condition(kind: str, *, target: str = "", region: str = "",
                    window: str = "", name: str = "", namespace: str = "",
                    image: str = "", field: str = "", equals: str = "",
                    pattern: str = "", min_count: int = 1,
                    log_baseline: int | None = None) -> dict:
    """**单次**求值一个验收条件。返回 `{hit, detail, error, unverifiable, baseline}`。

    为什么抽成独立函数：`wait_for`（轮询等待）与 `verify_state`（多条件验收）
    需要**同一套**条件语义。写两份实现必然漂移 —— 那样「同一个条件在两处
    给出不同结论」就会成为一个新的假信息源。AGENTS.md §3 要求一个能力
    只有一个实现。

    这里不做任何循环、不睡、不记证据 —— 那些是调用方的事。
    """
    hit = False
    detail: dict[str, Any] = {}
    error = ""
    unverifiable = ""
    baseline = log_baseline

    try:
        return _eval_condition_inner(
            kind, target=target, region=region, window=window, name=name,
            namespace=namespace, image=image, field=field, equals=equals,
            pattern=pattern, min_count=min_count, baseline=baseline)
    except Exception as exc:                                  # noqa: BLE001
        # 单次求值失败**不该**让整个等待崩掉 —— 记进 error 继续轮询。
        return {"hit": False, "detail": detail, "error": str(exc)[:200],
                "unverifiable": "", "baseline": baseline}


def _eval_condition_inner(kind: str, *, target: str = "", region: str = "",
                          window: str = "", name: str = "", namespace: str = "",
                          image: str = "", field: str = "", equals: str = "",
                          pattern: str = "", min_count: int = 1,
                          baseline: int | None = None) -> dict:
    """`_eval_condition` 的实际实现（外面那层只负责兜异常）。"""
    hit = False
    detail: dict[str, Any] = {}
    error = ""
    unverifiable = ""
    log_baseline = baseline

    if kind == "instances":
        r = RAW_TOOLS["find_instances"](
            name=name, namespace=namespace, image=image, max_count=200)
        if r.get("ok"):
            cnt = int(r.get("count") or 0)
            detail = {"count": cnt, "min_count": min_count}
            hit = cnt >= max(1, int(min_count))
        elif r.get("unsupported"):
            # 「本时机不可用」不是「没成立」
            unverifiable = str(r.get("error", ""))[:200]
            detail = {"unsupported": True}
        else:
            error = str(r.get("error", ""))[:200]

    elif kind == "field":
        fi = RAW_TOOLS["find_instances"](
            name=name, namespace=namespace, image=image, max_count=8)
        if not fi.get("ok"):
            if fi.get("unsupported"):
                unverifiable = str(fi.get("error", ""))[:200]
                detail = {"unsupported": True}
            else:
                error = str(fi.get("error", ""))[:200]
                detail = {"instances_ok": False}
        else:
            ptrs = [x.get("pointer") for x in (fi.get("instances") or [])]
            if not ptrs:
                detail = {"count": 0}
            else:
                rf = RAW_TOOLS["read_field_value"](
                    instance=ptrs[0], name=name, field=field,
                    namespace=namespace, image=image)
                if rf.get("ok"):
                    got = _field_value_as_text(rf)
                    detail = {"field": field, "actual": got, "expected": equals,
                              "pointer": ptrs[0]}
                    hit = _values_equal(got, equals, rf)
                else:
                    error = str(rf.get("error", ""))[:200]
                    detail = {"read_ok": False}

    elif kind == "ocr":
        o = RAW_TOOLS["ocr"](region=region, window=window, expected=target,
                             match="contains", max_items=50)
        if o.get("ok"):
            n = int(o.get("matched_count") or 0)
            detail = {"matched_count": n, "expected": target,
                      "texts_seen": len(o.get("texts") or [])}
            hit = n > 0
        else:
            error = str(o.get("error", ""))[:200]

    elif kind == "log":
        if not pattern:
            return {"hit": False, "detail": {}, "error": "kind='log' 需要 pattern",
                    "unverifiable": "", "baseline": baseline}
        if not target:
            # target 在 log 分支里是**日志路径**（与 ocr/window 的语义不同）
            return {"hit": False, "detail": {},
                    "error": "kind='log' 时请把日志路径放进 target",
                    "unverifiable": "", "baseline": baseline}
        lr = RAW_TOOLS["probe_log"](path=target, pattern=pattern, max_lines=20)
        if lr.get("ok"):
            n = int(lr.get("matched_count") or 0)
            if baseline is None:
                baseline = n
                detail = {"matched_count": n, "pattern": pattern,
                          "baseline": n, "baseline_note": "首次求值记基线，等增量"}
                hit = False
            else:
                detail = {"matched_count": n, "pattern": pattern,
                          "baseline": baseline, "delta": n - baseline}
                hit = n > baseline
        else:
            error = str(lr.get("error", ""))[:200]

    elif kind == "window":
        from .engine import screen as _screen
        w = _screen.find_window_rect(target)
        detail = {"window_found": bool(w.get("ok")), "title": w.get("title")}
        hit = bool(w.get("ok"))

    return {"hit": hit, "detail": detail, "error": error,
            "unverifiable": unverifiable, "baseline": baseline}


@mcp.tool(annotations=_ann("轮询等待一个条件成立（带证据）",
                           read_only=False, open_world=True,
                           destroy=False, idempotent=True))
@_cap_response
def wait_for(kind: str, target: str = "", region: str = "", window: str = "",
             name: str = "", namespace: str = "", image: str = "",
             field: str = "", equals: str = "", pattern: str = "",
             timeout_ms: int = 20000, interval_ms: int = 500,
             min_count: int = 1) -> dict:
    """**轮询等待条件成立并给出证据包**（验收闭环里「等到可以判了」那一步）。五种条件：`field` / `instances` / `ocr` / `log` / `window`。返回三态 `verdict`（`pass` / `fail` / **`unverifiable`** —— 把「没法判」与「没通过」严格区分）+ `polls` / `evidence`；**超时如实报 fail，不静默当通过**。注意 `kind="log"` 等的是**新增**（先记基线再等增量），不是「当前存在」。

    为什么必须有它：游戏是**异步**的 —— 点完按钮要等界面出来、场景加载要
    等几秒。不等待就直接断言，会得到大量**假失败**（其实只是还没加载完）。
    同类工具（MaaFramework）把它写成一句伪代码：
    `while(!hit && !timeout) { 检查; sleep(rate_limit); }`。

    支持的条件（`kind`）覆盖三类信号，**优先用引擎信号，少用像素**：

    | kind | 判据 | 需要哪些参数 |
    |---|---|---|
    | `field` | 某字段的值等于 `equals` | `name`/`field`/`equals`（`instance` 不需要：会先用 `find_instances` 找） |
    | `instances` | 某类的活实例数 ≥ `min_count` | `name`（可选 `namespace`/`image`） |
    | `ocr` | 屏幕上 OCR 到 `target` 文本 | `target`（可选 `region`/`window`） |
    | `log` | 日志里**新增**匹配 `pattern` 的行 | `target`=日志路径，`pattern` |
    | `window` | 出现标题含 `target` 的窗口 | `target` |

    **`kind='log'` 等的是「新增」，不是「存在」**（这条是真机实战踩出来的）：
    第一次轮询会记下基线匹配数，之后**超过**基线才算成立。
    否则旧日志里早就有的匹配行会让它在 **1ms** 内返回 `pass`，
    而本次操作根本没发生（实测：日志里已有 5 条「已修改 精核」时确实如此）。
    要判「当前是否已有」请直接用 `probe_log`，不要用 `wait_for`。

    **返回值就是证据包**（不是光秃秃的布尔）：
      · `ok` —— 条件成立了
      · `verdict` —— `pass` / `fail` / `unverifiable` 三态。
        `unverifiable` 表示「**没法判**」（例如依赖不可用、探针不上线），
        与 `fail`（判了、没成立）严格区分 —— 把「没法判」说成「没通过」
        会让 Agent 去修一个根本不存在的缺陷。
      · `polls` / `elapsed_ms` / `interval_ms` —— 轮询了几次、花了多久
      · `evidence` —— 每次轮询的实测值（最后若干次），可核查
      · `hint` —— 超时时给出下一步建议

    超时**如实报 `verdict=fail`**，不静默当成通过。

    Args:
        kind:        上面五种之一。
        target:      `ocr` / `window` 的目标文本。
        region:      `ocr` 用："x,y,w,h" 屏幕绝对坐标。
        window:      `ocr` 用：窗口标题子串。
        name/namespace/image: `field` / `instances` 的类定位参数。
        field:       `field` 用：字段名。
        equals:      `field` 用：期望值（按字段类型比较）。
        pattern:     `log` 用：日志正则。**必须同时给 `path`**。
        timeout_ms:  总超时（默认 20000）。
        interval_ms: 轮询间隔（默认 500）。别设太小 —— 徒增开销。
        min_count:   `instances` 用的实例数下限（默认 1）。

    Returns:
        {ok, verdict, kind, polls, elapsed_ms, evidence, last, hint?}
    """
    kinds = ("field", "instances", "ocr", "log", "window")
    if kind not in kinds:
        return _err(f"kind 只能是 {'/'.join(kinds)}，收到 {kind!r}")
    if kind == "log" and not target:
        return _err("kind='log' 时请把日志路径放进 target")
    timeout_ms = max(0, int(timeout_ms))
    interval_ms = max(50, int(interval_ms))
    deadline = time.time() + timeout_ms / 1000.0

    evidence: list[dict] = []
    polls = 0
    started = time.time()
    last_err = ""
    unverifiable_reason = ""
    #: `kind='log'` 的基线匹配数（见 `_eval_condition` 里「基线语义」的说明）
    log_baseline: int | None = None

    while True:
        polls += 1
        res = _eval_condition(
            kind, target=target, region=region, window=window, name=name,
            namespace=namespace, image=image, field=field, equals=equals,
            pattern=pattern, min_count=min_count, log_baseline=log_baseline)
        hit = bool(res["hit"])
        detail = res["detail"]
        log_baseline = res["baseline"]
        if res["error"]:
            last_err = res["error"]
        if res["unverifiable"]:
            unverifiable_reason = res["unverifiable"]

        evidence.append({"poll": polls, **detail})
        if len(evidence) > 30:            # 只留最近若干条，别把返回撑爆
            evidence = evidence[-30:]
        if hit:
            break
        if time.time() >= deadline:
            break
        time.sleep(interval_ms / 1000.0)

    elapsed = int((time.time() - started) * 1000)
    out: dict[str, Any] = {
        "ok": hit,
        "verdict": "pass" if hit else ("unverifiable" if unverifiable_reason
                                       else "fail"),
        "kind": kind,
        "polls": polls,
        "elapsed_ms": elapsed,
        "interval_ms": interval_ms,
        "timeout_ms": timeout_ms,
        "evidence": evidence,
        "last": evidence[-1] if evidence else {},
    }
    if not hit:
        out["error"] = (f"等待 {kind} 条件超时（{elapsed}ms / {polls} 次轮询）")
        if last_err:
            out["last_error"] = last_err
        if unverifiable_reason:
            out["unverifiable_reason"] = unverifiable_reason
            out["hint"] = ("这个条件**没法判**，不是「没通过」："
                           "引擎侧暂时不支持枚举实例。先确认游戏已进到"
                           "目标场景，或改用其它 kind（如 ocr / log）。")
        else:
            out["hint"] = ("超时可能因为：界面还没加载完 / 条件本身没被满足。"
                           "先看 evidence 里每次轮询的实测值再决定；"
                           "**不要**据此断定 Mod 无效。")
    return out


@mcp.tool(annotations=_ann("验收：多条件 + **≥2 条独立证据**才判成立",
                           read_only=False, open_world=True,
                           destroy=False, idempotent=True))
@_cap_response
def verify_state(checks: str, require_independent: int = 2) -> dict:
    """**出一份可证伪的验收结论**：每条断言独立求值，并要求**足够多的独立证据**才判成立。

    为什么必须有它（这是从 `reverse-skill` 的证据链契约学来的一条硬规则）
    ----------------------------------------------------------------
    单条证据**不得**静默提升为「已验证」。它那条契约的原文是：

    > `validated` 需要 **>=2 independent** Evidence（最好 1 静态 + 1 动态）；
    > 单条证据不得静默提升为 validated。

    我们自己的实战正好印证了这条：在 CatapultHero 验收 Mod 时，
    我同时看了**日志**（`已修改 精核 = 108`）与**屏幕 OCR**（`精核：108`）——
    两个**互相独立**的来源给出同一个结论，才敢说「Mod 真的生效了」。
    只看日志的话，日志本身是 Mod 打的，Mod 写错了照样打日志。

    所以本工具把「要几条独立证据」变成**显式参数**，而不是留给 Agent 的直觉。

    参数 `checks` 怎么传
    ----------------------------------------------------------------
    逗号分隔的条目，每条形如 `kind:参数`，与 `wait_for` 共用同一套条件语义
    （`_eval_condition`，所以两处结论必然一致）：

    | 写法 | 含义 |
    |---|---|
    | `log=<路径>#<正则>` | 日志里有匹配行（**只读不消费**） |
    | `ocr=<期望文本>` | 屏幕上 OCR 到该文本 |
    | `window=<标题子串>` | 存在该窗口 |
    | `field=<类>.<字段>=<期望值>` | 该字段当前等于期望值 |
    | `instances=<类>>=<最小个数>` | 该类活实例数达到下限 |

    示例（双证据验收 Mod 生效）：

        verify_state(checks="log=D:\\\\Game\\\\UE4SS.log#已修改 精核,"
                            "ocr=精核")

    返回
    ----------------------------------------------------------------
    · `verdict` —— `pass` / `fail` / **`unverifiable`**（三态，与 `wait_for` 同源）
      - `pass` 要求：**成立的条件数 >= require_independent**（默认 2）
      - 全部条件都成立但只有 1 条 → 判 **`fail`** 并说明「证据不足」，
        **不是** pass —— 这正是「单条证据不得静默提升」
    · `evidence` —— 每条断言各自的 `{check, hit, detail}`
    · `independent_count` / `require_independent`
    · `verdict_reason` —— **为什么**是这个结论（便于人工复核）

    Args:
        checks: 逗号分隔的断言列表，见上表。
        require_independent: 判 `pass` 所需的**成立条数**下限（默认 2）。
            设 1 会退化成「单条证据即通过」—— 只在确实只有一个可靠来源时用。

    Returns:
        {ok, verdict, verdict_reason, evidence, independent_count,
         require_independent, passed, unverifiable_count}
    """
    if not checks or not str(checks).strip():
        return _err("checks 不能为空。写法见工具文档，"
                    "例如 checks=\"log=D:\\\\x.log#ERROR,ocr=开始游戏\"")

    items = [x.strip() for x in str(checks).split(",") if x.strip()]
    require = max(1, int(require_independent))

    results: list[dict] = []
    for raw in items:
        if "=" not in raw:
            results.append({"check": raw, "hit": False, "kind": "",
                            "detail": {}, "error": "写法不对：应是 kind:参数"})
            continue
        kind, spec = raw.split("=", 1)
        kind = kind.strip().lower()
        spec = spec.strip()
        if kind not in ("log", "ocr", "window", "field", "instances"):
            results.append({"check": raw, "hit": False, "kind": kind,
                            "detail": {}, "error": f"未知的 kind {kind!r}"})
            continue

        kw: dict[str, Any] = {}
        note = ""
        try:
            if kind == "log":
                # log=<路径>#<正则>（用 # 分隔，避免正则里的逗号被当条目分隔符）
                if "#" not in spec:
                    raise ValueError("log 写法应是 log=<路径>#<正则>")
                path, pat = spec.split("#", 1)
                kw = {"target": path.strip(), "pattern": pat.strip()}
            elif kind == "ocr":
                kw = {"target": spec}
            elif kind == "window":
                kw = {"target": spec}
            elif kind == "field":
                # field=类.字段=期望值
                if "=" not in spec or "." not in spec.split("=", 1)[0]:
                    raise ValueError("field 写法应是 field=<类>.<字段>=<期望值>")
                lhs, want = spec.split("=", 1)
                cls, fname = lhs.rsplit(".", 1)
                # 类名可能带命名空间：最后一段是短名，前面是 namespace
                if "." in cls:
                    ns, short = cls.rsplit(".", 1)
                else:
                    ns, short = "", cls
                kw = {"name": short, "namespace": ns, "field": fname,
                      "equals": want.strip()}
            elif kind == "instances":
                # instances=类>=个数
                if ">=" not in spec:
                    raise ValueError("instances 写法应是 instances=<类>>=<个数>")
                cls, n = spec.split(">=", 1)
                cls = cls.strip()
                if "." in cls:
                    ns, short = cls.rsplit(".", 1)
                else:
                    ns, short = "", cls
                kw = {"name": short, "namespace": ns,
                      "min_count": max(1, int(n.strip()))}
        except Exception as exc:                              # noqa: BLE001
            results.append({"check": raw, "hit": False, "kind": kind,
                            "detail": {}, "error": f"解析失败：{exc}"})
            continue

        # 复用 wait_for 的**同一套**条件语义（`_eval_condition`），
        # 这样两处对同一条件不会给出不同结论。
        # 注意 `kind='log'` 在这里传 baseline=0：verify_state 判的是
        # 「日志里**有没有**该行」（当前状态），不是「有没有新增」
        # —— 与 wait_for 的等待语义不同，所以显式给 baseline。
        res = _eval_condition(kind, log_baseline=0 if kind == "log" else None,
                              **kw)
        rec = {"check": raw, "kind": kind, "hit": bool(res["hit"]),
               "detail": res["detail"]}
        if res["error"]:
            rec["error"] = res["error"]
        if res["unverifiable"]:
            rec["unverifiable"] = True
            rec["error"] = res["unverifiable"]
        if note:
            rec["note"] = note
        results.append(rec)

    passed = sum(1 for r in results if r["hit"])
    unver = sum(1 for r in results if r.get("unverifiable"))
    total = len(results)

    # ---- 三态判定（与 wait_for 同源的纪律）
    if passed >= require:
        verdict = "pass"
        reason = f"{passed}/{total} 条成立，达到要求的 {require} 条独立证据"
    elif unver >= 1 and passed + unver == total:
        # 没有一条明确不成立，但至少有没法判的 → 「没法判」而非「没通过」
        verdict = "unverifiable"
        reason = (f"成立 {passed} 条、**没法判** {unver} 条，"
                  f"达不到 {require} 条独立证据。这是「尺子坏了」而不是"
                  f"「量出来是坏的」—— 别据此改 Mod。")
    elif passed == 0:
        verdict = "fail"
        reason = f"0/{total} 条成立"
    else:
        # 有成立、但不够独立证据数 → **不许**提升为 pass
        verdict = "fail"
        reason = (f"只有 {passed} 条成立、需要 {require} 条**独立**证据 —— "
                  f"**证据不足，不判通过**。单条证据不得静默提升；"
                  f"请再加一条**互相独立**的来源（例如日志 + 屏幕 OCR）。")

    out: dict[str, Any] = {
        "ok": verdict == "pass",
        "verdict": verdict,
        "verdict_reason": reason,
        "passed": passed,
        "total": total,
        "independent_count": passed,
        "require_independent": require,
        "unverifiable_count": unver,
        "evidence": results,
    }
    if verdict != "pass":
        out["error"] = reason
    return out


def _field_value_as_text(rf: dict) -> str:
    """把 `read_field_value` 的结果压成一个可比较的字符串。

    按**实际读取宽度**取值，不给这个宽度下没意义的解释
    （与 `read_field_value` 自己的口径一致）：4 字节字段绝不拼 8 字节。
    """
    w = int(rf.get("read_width") or 8)
    if w == 1:
        if "as_uint8" in rf:
            return str(rf["as_uint8"])
    elif w == 2:
        if "as_uint16" in rf:
            return str(rf["as_uint16"])
    elif w == 4:
        if "as_int32" in rf:
            return str(rf["as_int32"])
    else:
        if "as_int64" in rf:
            return str(rf["as_int64"])
    raw = str(rf.get("raw_hex") or "")
    return raw


def _values_equal(actual: str, expected: str, rf: dict) -> bool:
    """比较字段实际值与期望值。按类型做**语义**比较，不做字符串比较。

    为什么不能只比字符串：`equals="9999"` 与实际的 `as_int32=9999`
    字符串比较是相等的，但 `equals="0x270F"`（同一个数的十六进制）就
    不会相等 —— 而调用方完全可能这么写。浮点也一样（`1.0` vs `1`）。
    """
    if actual is None or expected is None:
        return False
    a, e = str(actual).strip(), str(expected).strip()
    if a == e:
        return True
    ftype = str(rf.get("field_type") or "")
    # 整数族：两边都按整数解析（支持 0x 前缀）
    if ftype and ("Int" in ftype or "Byte" in ftype or "Char" in ftype
                  or "Boolean" in ftype):
        try:
            return int(a, 0) == int(e, 0)
        except (ValueError, TypeError):
            pass
    if ftype in ("System.Single", "System.Double", "float", "double"):
        try:
            return abs(float(a) - float(e)) < 1e-6
        except (ValueError, TypeError):
            pass
    return False


# ================================================================
# 7. Unreal 引擎层
# ================================================================
#
# UE 没有 IL2CPP 那样的集中元数据文件，所以这一节的工具不是「查类型」，
# 而是「查资源容器」：pak / utoc 里到底打包了哪些资产。
# 已实测验证的能力（对两个真实样本与打包清单交叉校验 100% 命中）：
#   · 识别 UE 游戏
#   · 解析 .pak v11/v12 索引 → 完整文件清单
#   · 解析 .utoc v8 目录索引 → 完整文件清单（含 UTF-16 中文路径）
#   · 打包清单 Manifest_*.txt
#   · exe 内的 /Script/ 模块与对象路径
# **未实现**（诊断里会如实标注）：FName 池、UObject 属性偏移、.usmap。
# 这几项在静态样本里拿不到，需要运行时；不要暗示 Agent 它们可用。


def _ue_exe(exe: str) -> tuple[str, dict | None]:
    """解析出 UE 游戏的 exe：显式给了就用，否则从当前会话推。"""
    target = exe
    if not target:
        st = state.state()
        target = st.exe
        if not target and st.game_dir:
            ue = discovery.ue_paths(st.game_dir)
            target = ue.get("exe") or discovery.pick_main_exe(st.game_dir)
    if not target or not Path(target).is_file():
        return "", _err(
            f"找不到 UE 游戏主程序：{target or '(未指定)'}",
            hint="传 exe 参数，或先用 open_game/inspect_game 选定 UE 游戏",
        )
    return target, None


@mcp.tool(annotations=_ann("识别 Unreal 游戏", read_only=True))
@_cap_response
def detect_ue(exe: str = "") -> dict:
    """判断一个 exe 是否为 Unreal 引擎游戏，并给出可核查的证据与置信度。

    只判定**这一个**程序，不做全机扫描。

    **UE 版本号拿不到是正常的**：实测两个样本中 exe 内 UE 版本模式 0 命中，
    Engine/Build/Build.version 也不存在。此时返回空串 + 原因，不猜测。

    Args:
        exe: 游戏可执行文件路径。留空则用当前会话的主程序。
    """

    target, err = _ue_exe(exe)
    if err:
        return err
    try:
        res = ue_mod.detect_ue_game(target)
    except Exception as exc:
        return _err(f"识别失败：{exc}", exe=target)
    return _ok(**res)


@mcp.tool(annotations=_ann("提取 Unreal 游戏概况", read_only=False, destroy=False, idempotent=True))
@_cap_response
def ue_profile(exe: str = "", output_path: str = "") -> dict:
    """**UE 游戏概况**：容器、清单、**脚本模块**。汇总一个 Unreal 游戏的可提取信息（资源容器、清单、脚本对象）。

    包含每个 .pak/.utoc 的文件数、目录数、以及条目样例。
    全量条目可能非常大（实测某样本 18,956 条 → JSON 15.8 MB），
    所以默认只给样例与计数；要完整清单用 ue_list_entries。

    Args:
        exe:         游戏可执行文件路径。留空则用当前会话的主程序。
        output_path: 可选，把完整 profile 写成 JSON 文件的路径。
    """

    target, err = _ue_exe(exe)
    if err:
        return err
    try:
        res = ue_mod.extract_ue_profile(
            target, output_path=output_path or None, include_entries=False,
        )
    except Exception as exc:
        return _err(f"提取失败：{exc}", exe=target)
    return _ok(**res)


@mcp.tool(annotations=_ann("列出 Unreal 资源容器条目", read_only=True))
@_cap_response
def ue_list_entries(container: str, kind: str = "auto",
                    pattern: str = "", limit: int = 200,
                    offset: int = 0) -> dict:
    """列出 .pak / .utoc 里**打包了哪些资产**（可按名字过滤、翻页）。返回 `returned`/`has_more`；翻页空页会说明是哪一种「空」。

    这是 UE 游戏上最实用的一步：想知道「这游戏有哪些资源、
    本地化文本在哪个文件里」，就靠它。

    Args:
        container: .pak 或 .utoc 文件路径。
        kind:      auto / pak / utoc。默认按扩展名自动判断。
        pattern:   名字子串过滤（大小写不敏感）。
        limit:     最多返回多少条。
        offset:    从第几条开始（配合 total 翻页）。
    """

    p = Path(container)
    if not p.is_file():
        return _err(f"文件不存在：{container}")
    k = kind
    if k == "auto":
        k = "utoc" if p.suffix.lower() == ".utoc" else "pak"

    try:
        if k == "utoc":
            # list_*_entries 在「解析失败」和「容器真的是空」两种情况下
            # 都会返回空列表。必须回到 read_*_index 看 ok/reason 才能区分，
            # 否则一个损坏/非 pak 的文件会被报成「解析成功但没条目」——
            # 假成功是最容易骗到 Agent 的一类结果。
            #
            # 把 report 传给 list_*_entries 复用，不要让它再解析一遍：
            # 实测同一容器解析两次占该工具耗时 48%~52%（大容器上过百毫秒）。
            report = ue_mod.read_iostore_index(str(p))
            if not report.get("ok"):
                return _err(
                    f"容器解析失败：{report.get('reason') or '未知原因'}",
                    container=str(p), kind=k,
                    file_size=report.get("file_size"),
                )
            entries = ue_mod.list_iostore_entries(str(p), report=report)
        elif k == "pak":
            report = ue_mod.read_pak_index(str(p))
            if not report.get("ok"):
                return _err(
                    f"容器解析失败：{report.get('reason') or '未知原因'}",
                    container=str(p), kind=k,
                    file_size=report.get("file_size"),
                )
            entries = ue_mod.list_pak_entries(str(p), report=report)
        else:
            return _err(f"kind 只能是 auto/pak/utoc，收到：{kind}")
    except Exception as exc:
        return _err(f"解析失败：{exc}", container=str(p), kind=k)

    total = len(entries)
    filtered_entries = entries
    if pattern:
        low = pattern.lower()
        filtered_entries = [
            e for e in entries if low in (e.get("path") or "").lower()
        ]

    matched = len(filtered_entries)
    limit = max(1, limit)
    page = filtered_entries[offset:offset + limit]
    # `returned` 与全项目其它列表工具（_paged / list_images / find_derived_classes）
    # 统一；以前这里叫 `count`，Agent 按惯例读 returned 只会拿到 None。
    out = _ok(container=str(p), kind=k, total=total,
              matched=matched, offset=offset, limit=limit,
              returned=len(page),
              has_more=offset + len(page) < matched, entries=page)
    if not page and total:
        # 容器有内容但这一页是空的：说清到底是哪种「空」
        if pattern and matched == 0:
            out["note"] = (f"容器解析成功（共 {total} 条），但过滤条件 "
                           f"{pattern!r} 没有命中任何条目")
        elif offset >= matched:
            # 关键分支：offset 越过的是**过滤后的**条数，不是总条数。
            # 以前判的是 offset >= total，于是 matched=1511 而 offset=6000
            # （6000 < 21439）时既不空说明也不给 has_more，
            # 调用方拿到空 entries 却毫无解释。
            out["note"] = (f"offset={offset} 已超出匹配到的 {matched} 条"
                           f"（容器共 {total} 条），这一页为空。"
                           f"请用 offset<{matched} 翻页。")
    elif total == 0:
        out["note"] = "容器解析成功，但里面确实没有任何条目。"
    return out


@mcp.tool(annotations=_ann("列出 Unreal 脚本对象", read_only=True))
@_cap_response
def ue_script_objects(exe: str = "", limit: int = 200) -> dict:
    """从 UE 游戏 exe 里提取 /Script/ 模块名与对象路径。失败时按契约给 `ok=false` + `error`。

    这是**字符串证据**，不是反射元数据：能告诉你游戏大致有哪些系统
    （如 /Script/Engine、/Script/GameplayAbilities），
    但**拿不到父类链和属性偏移**——那需要运行时。

    Args:
        exe:   游戏可执行文件路径。留空则用当前会话的主程序。
        limit: 每类最多返回多少条。
    """

    target, err = _ue_exe(exe)
    if err:
        return err
    try:
        res = ue_mod.list_script_objects(target)
    except Exception as exc:
        return _err(f"提取失败：{exc}", exe=target)

    out = dict(res) if isinstance(res, dict) else {"result": res}
    # 结果可能很长，按 limit 截断，但保留原始计数
    for key in ("modules", "objects"):
        v = out.get(key)
        if isinstance(v, list):
            out[f"{key}_total"] = len(v)
            out[key] = v[:max(1, limit)]
    out.setdefault(
        "note",
        "这些是 exe 内的字符串证据，不是反射元数据；"
        "父类链与属性偏移无法由此获得。",
    )
    # 契约第 1 条：失败必须带 error。底层 ue.py 用的是 reason，
    # 这里转成约定的 error 字段（并保留 reason，不丢信息），
    # 否则按契约只读 error 的调用方会把失败当成正常返回。
    if not out.get("ok", True):
        reason = out.get("reason") or "未能在 exe 中提取到 /Script/ 对象"
        payload = {k: v for k, v in out.items() if k not in ("ok", "reason")}
        payload["reason"] = reason
        return _err(reason, **payload)
    return _ok(**out)


@mcp.tool(annotations=_ann("读取 Unreal 打包清单", read_only=True))
@_cap_response
def ue_manifest(path: str = "", game_dir: str = "", pattern: str = "",
                offset: int = 0, limit: int = 200) -> dict:
    """读取 UE 的打包清单 `Manifest_*Files*.txt`。支持 `offset`/`limit` 并有 `has_more`。

    清单是打包时生成的「应该有哪些文件」的真值，
    常用来校验从 pak/utoc 解析出的文件清单是否正确。

    Args:
        path:     清单文件路径。留空则自动在 game_dir 里找。
        game_dir: 游戏安装目录（path 留空时用）。
        pattern:  路径子串过滤。
        offset:   从第几条开始（翻页用）。
        limit:    本页最多多少条。被截断时 `has_more` 为 true。
    """

    target = path
    #: 自动挑清单时是否命中了首选（UFS 清单）。显式给 path 时恒为 True
    #: —— 调用方自己指定了，就没有「挑错」这回事。
    picked_preferred = True
    if not target:
        root = game_dir
        if not root:
            root = state.state().game_dir
        if not root:
            return _err("需要提供 path 或 game_dir")
        found = ue_mod.find_manifests(root)
        if not found:
            return _err(f"在 {root} 下没找到打包清单"
                        f"（Manifest_*Files*.txt）", game_dir=root)
        # 不能直接取 found[0]：`find_manifests` 是字母序的，第一个是
        # Manifest_DebugFiles（实测只有 3 条），于是任何 pattern 都会「零命中」，
        # 而 note 会让调用方以为「包里没有」。优先挑 UFS 清单，见 pick_manifest。
        target, picked_preferred = ue_mod.pick_manifest(found)

    if not Path(target).is_file():
        return _err(f"文件不存在：{target}")
    try:
        res = ue_mod.read_manifest(target)
    except Exception as exc:
        return _err(f"读取失败：{exc}", path=target)

    if not isinstance(res, dict):
        res = {"manifest": res}
    entries = res.get("files") or res.get("entries") or []
    total = len(entries)
    if pattern:
        low = pattern.lower()
        entries = [e for e in entries
                   if low in (str(e) if not isinstance(e, dict)
                              else str(e.get("path", ""))).lower()]
    res["total"] = total
    res["matched"] = len(entries)
    key = "files" if "files" in res else "entries"
    offset = max(0, offset)
    limit = max(1, limit)
    page = entries[offset:offset + limit]
    res["offset"] = offset
    res["limit"] = limit
    res["returned"] = len(page)
    # 如实标注截断：以前只把列表切一刀，Agent 无从判断是否还有后续
    res["has_more"] = offset + len(page) < len(entries)
    res[key] = page
    if pattern and not entries:
        # 过滤零命中必须给说明，不能只给一个空列表。
        # 而且必须**说清用的是哪一份清单** —— 否则「零命中」会被读成
        # 「包里没有」，而实际可能只是清单挑错了（见 pick_manifest）。
        res["note"] = (f"清单 {Path(target).name} 解析成功（共 {total} 条），"
                       f"但过滤条件 {pattern!r} 没有命中任何条目；"
                       f"去掉 pattern 可看全部。")
    if not picked_preferred:
        # 没挑到 UFS 清单 = 这份清单大概率不是「打进 pak 的内容」。
        # 如实说出来，别让调用方把它的「零命中」当成结论。
        warn(f"{target} 不是 Manifest_UFSFiles_*，可能是旁挂文件清单")
        extra = (f"注意：自动挑选时**没找到** Manifest_UFSFiles_* 清单，"
                 f"退回用了 {Path(target).name} —— 它记录的是不进 pak 的旁挂文件，"
                 f"与资源包内容无关。要查打包内容请显式指定 path=")
        res["note"] = f"{res.get('note')}；{extra}" if res.get("note") else extra
        res["picked_preferred"] = False
    return _ok(**res)


# ================================================================
# 8. 诊断层
# ================================================================


@mcp.tool(annotations=_ann("环境与能力诊断", read_only=True))
@_cap_response
def get_diagnostics(scan_processes: bool = False) -> dict:
    """检查运行环境：Python、Frida、各引擎解析器是否可用。**默认不扫本机进程**（见 `scan_processes`）。

    开始工作前建议先调一次，明确「这台机器上能做什么」。
    如果某项显示不可用，对应的工具会失败——这是环境问题，不是工具 bug。

    Args:
        scan_processes: 是否顺带列出本机正在运行的游戏。默认 **False**。
            该扫描实测耗时约 2.7 秒，且与 `find_game_processes` 职责重复，
            所以默认不扫 —— 要列运行中的游戏请直接调 `find_game_processes`。
    """
    try:
        info = state.diagnostics(scan_processes=scan_processes)
        info["version"] = __version__
        return _ok(**info)
    except Exception as exc:
        return _err(f"诊断失败：{exc}")


# ================================================================
# 9. 资源（MCP resources）
# ================================================================
#
# 只做一个资源，理由写在模块 docstring 的「资源」一节：
# 工具已经能给出的一切都不该再开第二个出口（§3.1 第 3 条）。

_GUIDE_REL = Path("skills") / "gamemod-mod" / "SKILL.md"


def _guide_candidates() -> list[Path]:
    """`gamemod://guide` 的正文按此优先级去找。

    覆盖两种部署形态：
      · 在仓库里跑 —— `server.py` 往上两级就是仓库根；
      · 在别处跑但装过 skill —— skill 会被复制到 `~/.dsh/skills/`。
    """
    here = Path(__file__).resolve()
    out = [p / _GUIDE_REL for p in here.parents[1:3]]      # src/ 与 仓库根
    out.append(Path.home() / ".dsh" / _GUIDE_REL)
    return out


def guide_text() -> str:
    """`gamemod://guide` 的正文（纯函数，便于单测）。

    找不到文件时**不返回空串、也不假装成功**，而是如实列出找过的位置
    并给出退路（§3.1 第 4 条：找不到就明说）。
    """
    tried: list[str] = []
    for path in _guide_candidates():
        tried.append(str(path))
        try:
            if path.is_file():
                return path.read_text(encoding="utf-8")
        except OSError as exc:                              # 权限/占用等
            tried.append(f"（读取失败：{exc}）")
    listed = "\n".join(f"- {t}" for t in tried)
    return (
        "# GameMod MCP 使用指南（当前环境里找不到）\n"
        "\n"
        "这份资源的内容来自 `skills/gamemod-mod/SKILL.md`，"
        "但下面这些位置都没有它：\n"
        f"{listed}\n"
        "\n"
        "不影响使用：`instructions` 里已有七步工作流，各工具的说明也在"
        "各自 docstring 里。先 get_diagnostics 确认这台机器能做什么，"
        "再 open_game 选定游戏即可。\n"
    )


@mcp.resource(
    "gamemod://guide",
    name="gamemod_guide",
    title="GameMod MCP 使用指南",
    description="本 MCP 的完整工作流说明（与 skills/gamemod-mod/SKILL.md 同源）。",
    mime_type="text/markdown",
)
def guide_resource() -> str:
    """完整使用指南：七步工作流、配方、引擎差异、常见坑。"""
    return guide_text()


# ================================================================
# 事件循环保护：把同步工具 offload 到线程
# ================================================================
#
# **这是缺陷 #1 的修复。**
#
# 问题（实测，不是推测）：本文件的 38 个工具**全部**是同步 `def`
# （`async def` 计数为 0）。而 mcp 1.26.0 的
# `fastmcp/utilities/func_metadata.py:92-95` 对同步函数是
#
#     if fn_is_async: return await fn(**kwargs)
#     else:           return fn(**kwargs)          # ← 直接在事件循环线程里跑
#
# —— 没有 `to_thread`、没有 `run_in_threadpool`。于是任何慢工具都会**占住
# 整个 asyncio 事件循环**，期间服务器不再读任何请求。实测
# （`devtools/archive/probes/blocking_check.py`）：一个真建索引的调用
# （2.57s）在飞时，连**完全不碰 Frida**、只读内存状态的 `get_session`
# 都拿不到响应（1.5s 超时）。
#
# 为什么不在每个工具上写 `async def` + `await asyncio.to_thread(...)`：
# 那要改 38 个函数，且会让 **64 处**「脚本内直接 `S.open_game(...)`」的
# 现有验收全部变成「忘了 await 的协程」—— 测试会静默拿到协程对象而不是
# 结果。改 SDK 注册层的这一处，`server.<工具>` 仍然是可同步调用的普通函数，
# 只有**经 MCP 协议分发**的那条路被 offload，两条路的语义都正确。
#
# 做法：包住 `ToolManager.call_tool`（FastMCP 分发工具的唯一入口），
# 把其中的同步 `tool.run(...)` 丢进线程执行。
# `asyncio.to_thread` 是**标准库**（AGENTS.md §2.4：新增第三方依赖要先说明），
# 不引入任何新依赖。


def _install_event_loop_guard() -> bool:
    """让经 MCP 分发的同步工具在线程里跑，不阻塞事件循环。

    Returns:
        True = 装上了；False = 当前 mcp 版本的结构与预期不符，
        **没有**装上（会打警告，不抛异常 —— 旧版本 mcp 下服务器仍应能工作，
        只是退回到「慢工具会阻塞」的旧行为）。
    """
    try:
        from mcp.server.fastmcp.tools.tool_manager import ToolManager
    except Exception as exc:  # noqa: BLE001 - mcp 结构变了
        warn(f"无法安装事件循环保护（import ToolManager 失败）：{exc}")
        return False

    original = getattr(ToolManager, "call_tool", None)
    if original is None or getattr(original, "_gamemod_offload", False):
        return False

    async def call_tool_offloaded(self: Any, name: str, arguments: dict,
                                  context: Any = None,
                                  convert_result: bool = False) -> Any:
        """与上游 `ToolManager.call_tool` 等价，只把同步执行挪到线程。

        **只对同步工具 offload**：异步工具本来就是 await 的，
        再套一层线程反而会破坏它们对事件循环的假设。
        """
        tool = self.get_tool(name)
        if tool is None:
            raise ToolError(f"Unknown tool: {name}")
        if getattr(tool, "is_async", False):
            return await tool.run(arguments, context=context,
                                  convert_result=convert_result)
        return await asyncio.to_thread(
            tool.run_sync_for_offload, arguments, context, convert_result)

    #: 给 `Tool` 补一个**同步**执行入口。
    #:
    #: 为什么不直接 `await tool.run(...)`（在上游那条路上）或
    #: `asyncio.run(tool.run(...))`：`tool.run` 内部除了调用工具函数，
    #: 还要做参数校验与结果转换。这里显式复刻它对**同步函数**的那条分支
    #: （`fn_is_async=False`），语义与上游逐字一致，只是换了个线程执行。
    #:
    #: `call_fn_with_arg_validation` 本身是 `async def`，所以即便走同步分支
    #: 也要 await 一次才能拿到值 —— 在 worker 线程里用 `asyncio.run` 收口。
    #: 那条分支内部**只**执行 `fn(**kwargs)`，不会碰 `context` 绑定的原循环
    #: （工具函数本身也不接受 context，见 `_ann` 之外的签名）。
    def run_sync_for_offload(self: Any, arguments: dict, context: Any,
                             convert_result: bool) -> Any:
        async def _once() -> Any:
            return await self.fn_metadata.call_fn_with_arg_validation(
                self.fn, False, arguments,
                {self.context_kwarg: context} if self.context_kwarg is not None
                else None,
            )
        try:
            result = asyncio.run(_once())
            if convert_result:
                result = self.fn_metadata.convert_result(result)
            return result
        except Exception as e:      # noqa: BLE001 - 与上游一致：包成 ToolError
            raise ToolError(f"Error executing tool {self.name}: {e}") from e

    Tool.run_sync_for_offload = run_sync_for_offload       # type: ignore[attr-defined]
    call_tool_offloaded._gamemod_offload = True            # type: ignore[attr-defined]
    ToolManager.call_tool = call_tool_offloaded            # type: ignore[method-assign]
    return True


#: 模块导入时就装上。放在这里而不是 `main()` 里：以 `-m` 或脚本方式启动都会
#: 执行到这里，而 `main()` 只在 `__main__` 分支跑（验证脚本 import 本模块时
#: 不会走 main，但它们的 stdio 子进程会 —— 两边都要保护到）。
#:
#: 注意：**不改** `server.<工具>` 这些模块属性，所以
#: `devtools/verify_*.py` 里 `S.open_game(...)` 那 64 处同步调用照旧工作。
_EVENT_LOOP_GUARD = _install_event_loop_guard()


# ================================================================
# 入口
# ================================================================


def main() -> None:
    """以 stdio 方式启动 MCP 服务器（MCP 客户端的标准接入方式）。"""
    progress(f"GameMod MCP v{__version__} 启动（stdio）")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
