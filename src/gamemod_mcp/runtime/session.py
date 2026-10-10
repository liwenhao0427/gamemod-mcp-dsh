# Frida 会话管理：注入、RPC 调用、事件收集、生命周期
#
# 设计要点
#   · 与 AiMod 的「会话锁 / 单会话」语义对齐：同一进程只保留一条会话，
#     重复 attach 复用已有会话而不是叠加注入（叠加会导致 Hook 重复触发）。
#   · Hook 事件通过 send() 异步推送，这里用队列缓冲，由上层按需 drain。
#     **不能让回调直接阻塞**，否则会拖慢甚至卡死游戏线程。
#   · 会话不跨进程复用：进程退出（detached）后标记失效，下次调用重新注入。
from __future__ import annotations

import json
import os
import queue
import threading
import time
from pathlib import Path
from typing import Any

from ..logutil import debug, progress, warn

# ---------------------------------------------------------------- 异常


class SessionError(RuntimeError):
    """会话层通用错误。"""


class RuntimeUnavailable(SessionError):
    """Frida 不可用（未安装 / 无权限 / 目标架构不匹配）。"""


# ---------------------------------------------------------------- Frida 懒加载

_frida = None
_frida_error: str | None = None


def _load_frida():
    """延迟导入 frida。

    这样即使运行环境没有 frida（只想用静态元数据功能），
    也不会在 import 阶段就失败。
    """
    global _frida, _frida_error
    if _frida is not None:
        return _frida
    if _frida_error is not None:
        raise RuntimeUnavailable(_frida_error)
    try:
        import frida  # noqa: PLC0415  运行时按需导入
    except Exception as exc:  # pragma: no cover - 取决于部署环境
        _frida_error = (
            f"未安装 frida（{exc}）。运行时注入/Hook 功能不可用；"
            "静态元数据查询不受影响。安装：pip install frida"
        )
        raise RuntimeUnavailable(_frida_error) from exc
    _frida = frida
    return _frida


def frida_version() -> str | None:
    """返回 frida 版本号；未安装返回 None（不抛异常）。"""
    try:
        return _load_frida().__version__
    except RuntimeUnavailable:
        return None


def probe_available() -> dict:
    """探测运行时能力，供 get_diagnostics 使用。"""
    try:
        frida = _load_frida()
    except RuntimeUnavailable as exc:
        return {"available": False, "reason": str(exc)}
    info: dict[str, Any] = {"available": True, "version": frida.__version__}
    try:
        dev = frida.get_local_device()
        info["device"] = dev.name
        # 列出可注入进程数量，确认有权限
        try:
            procs = dev.enumerate_processes()
            info["process_count"] = len(procs)
        except Exception as exc:
            info["process_count"] = None
            info["enumeration_error"] = str(exc)
    except Exception as exc:
        info["available"] = False
        info["reason"] = f"无法访问本地设备：{exc}"
    return info


# ---------------------------------------------------------------- 探针脚本

_AGENT_PATH = Path(__file__).with_name("agent.js")


def agent_source() -> str:
    """读取探针脚本源码。

    **故意不做缓存**（每次注入都重新读盘）。这里曾经缓存到模块级全局，
    后果是：长时间运行的 MCP 服务器改了 `agent.js` 之后**永远**注入旧探针，
    而磁盘上是新代码 —— 调试的人对着新代码找问题，跑的是旧行为。
    实测就踩到过：端到端测试里 `agent.js` 的 Mono 后端明明有 53 个导出绑定，
    live 服务器注入的却是只认 IL2CPP 的旧脚本，报的还是旧提示语
    （「未找到 IL2CPP 运行时模块」），让人以为是新代码坏了。
    注入是低频操作（一次会话一次），读一个 100 KB 文本的代价可以忽略，
    换来的是「磁盘上的探针 == 注入的探针」这条不变量。
    """
    if not _AGENT_PATH.is_file():
        raise SessionError(f"探针脚本缺失：{_AGENT_PATH}")
    return _AGENT_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------- 重载选择


# .NET 类型名 → 探针的读值规格。
#
# 为什么需要它：返回值读法**不能**一律按指针。x64 上 float / double 的返回值
# 在 XMM0，按 ptr 去读 RAX 会给出一个看着像地址、其实毫无意义的整数
# （实测 Mathf.Abs(-2.5f) 的 retval 是 0x20da0a48360）。整型家族按各自宽度读，
# 引用类型/结构体/枚举一律给 ptr（枚举的底层整型值就在 RAX 里，读出来即原值）。
_RETVAL_SPECS = {
    "System.Single": "float",
    "System.Double": "double",
    "System.Boolean": "bool",
    "System.Int32": "int",
    "System.UInt32": "uint",
    "System.Int16": "int",
    "System.UInt16": "uint",
    "System.SByte": "int",
    "System.Byte": "uint",
    "System.Char": "uint",
    "System.Int64": "long",
    "System.UInt64": "ulong",
    "System.String": "string",
}


def _retval_spec_for(return_type: Any) -> str:
    """把 .NET 返回类型名映射成探针的读值规格；认不出来就给 `ptr`。

    认不出来时给 `ptr` 而不是猜：`ptr` 至少是「原样给出寄存器值」这个
    不含糊的语义，调用方可以自己判断。
    """
    if not return_type:
        return "ptr"
    return _RETVAL_SPECS.get(str(return_type).strip(), "ptr")


def _norm_token(tok: Any) -> str:
    """把 token 归一化成 8 位小写十六进制，便于跨表示比较。

    静态元数据给的是 `0x06000A3D` / `"0x06000a3d"`，运行时给的是
    `"0x06000a3d"`，而探针在拿不到 token 时会给 `None`。
    这里统一成 `"06000a3d"`，避免因大小写或 0x 前缀差异误判「不是同一个方法」。
    """
    if tok is None:
        return ""
    s = str(tok).strip().lower()
    if s.startswith("0x"):
        s = s[2:]
    if not s or s in ("none", "null", "0"):
        return ""
    return s.rjust(8, "0")


def _select_overload(usable: list[dict], *, token: str, param_count: int,
                     name: str, method: str) -> tuple[dict | None, dict | None]:
    """从多个可 Hook 的候选中挑出唯一一个。

    返回 `(选中的候选, 错误字典)`，两者恰有一个非 None。

    为什么不允许「拿不准就取第一个」：同名重载的 token 不同、语义也不同。
    盲选会挂到错误的方法上，而 Hook 本身会成功 ——
    于是错误以「数据看起来不对」的形式在很久之后才暴露，极难排查。
    宁可明确失败并给出候选清单，让调用方按 token 决定。
    """
    if token:
        want = _norm_token(token)
        matched = [c for c in usable if _norm_token(c.get("token")) == want]
        if not matched:
            avail = [c.get("token") for c in usable]
            return None, {
                "ok": False,
                "error": f"{name}.{method} 的候选中没有 token={token} 的方法"
                         f"（可用：{avail}）",
                "candidates": usable,
            }
        if len(matched) > 1:
            # token 唯一标识方法，理论上不会重；真出现说明探针数据异常，
            # 这种情况不猜，直接报出来。
            return None, {
                "ok": False,
                "error": f"{name}.{method} 有 {len(matched)} 个候选 token 都是 {token}",
                "candidates": matched,
            }
        return matched[0], None

    if param_count is not None and param_count >= 0:
        matched = [c for c in usable if c.get("param_count") == param_count]
        if not matched:
            avail = [c.get("param_count") for c in usable]
            return None, {
                "ok": False,
                "error": f"{name}.{method} 的候选中没有 {param_count} 个参数的重载"
                         f"（可用参数个数：{avail}）",
                "candidates": usable,
            }
        if len(matched) > 1:
            return None, {
                "ok": False,
                "error": f"{name}.{method} 有 {len(matched)} 个重载都是 "
                         f"{param_count} 个参数，无法凭参数个数区分；"
                         "请改用 token 指定",
                "candidates": matched,
            }
        return matched[0], None

    if len(usable) == 1:
        return usable[0], None

    # 多个候选、又没给选择依据 —— 拒绝静默猜测
    return None, {
        "ok": False,
        "error": f"{name}.{method} 有 {len(usable)} 个重载，必须指定 token "
                 "或 param_count 才能挂对。请先用 find_hook_target 看清候选，"
                 "再从静态元数据里取目标重载的 token。",
        "candidates": usable,
        "hint": "hook_method(token=\"0x06000a3d\") 或 hook_method(param_count=1)",
    }


# ---------------------------------------------------------------- 会话

# 进程 pid → RuntimeSession；配合锁保证同一进程只注入一次
_sessions: dict[int, "RuntimeSession"] = {}
_sessions_lock = threading.RLock()


#: 单次探针 RPC 的默认超时（秒）。
#:
#: **为什么必须有这个数字**：Frida 的同步 RPC（`script.exports_sync.x()`）
#: **没有超时参数**，一次不返回的调用会把调用方永久挂住。实测踩到的现场：
#: `launch_game` 通过 DSH 调用后 **11 分钟不返回**，期间连不碰 Frida 的
#: `get_session` 也一直超时（因为工具是同步的，整个事件循环被占住）；
#: 被启动的游戏窗口标题停在 `Fatal error in GC`，而 `wait_ready` 的
#: 60 秒预算**完全失效** —— 它的 deadline 只在每次调用**返回之后**才检查。
#:
#: 取 30 秒的依据：实测正常探针调用都在毫秒级（`runtime_class_info` 0.27s、
#: `hook_method` 0.01s、`list_images` 0.01s），最慢的 `inject_process`
#: 也不过 2.4 秒（等 Mono 程序集稳定）。30 秒是两个数量级的余量，
#: 正常操作绝不会误判，而卡死时能在半分钟内如实报错。
CALL_TIMEOUT = 30.0


#: spawn 之后**第一次**探测前先静默多久（秒）。
#:
#: 依据（`devtools/archive/probes/wedge_ab.py` 的对照实验，Ved 上各 8 轮）：
#:
#: | 第一次探测前的静默 | 探测间隔 | 卡死率 |
#: |---|---|---|
#: | 0 秒 | 0（紧循环） | **8/8** |
#: | 0 秒 | 0.1 秒 | 1/8 |
#: | 0 秒 | 0.5 秒 | 0/8 |
#: | **3 秒** | **0（紧循环）** | **0/8** |
#:
#: 最后一行是这条常量的由来：即使把探测频率拉到最激进，
#: 只要**第一次探测**推迟到游戏启动 3 秒之后，就不再出现卡死。
#: 也就是说危险窗口在「游戏启动后的头几秒」，而不是「探测次数多」本身。
#:
#: **这是一条缓解措施，不是根因修复**（务必分清）：
#: 根因（为什么在早期探测会让游戏进 `Fatal error in GC`）仍未定位，
#: 且产品路径在 0.5 秒间隔下**仍然卡死过两次**，说明本常量之外还有别的因素。
#: 详见 `docs/重启验证与运行时理解报告.md` §7.2 / §7.3。
#: 真正兜底的是 `CALL_TIMEOUT`：无论卡不卡，都不会再无限挂住。
READY_SETTLE_DELAY = 3.0


class RuntimeSession:
    """一条指向目标进程的 Frida 会话。"""

    #: 类级默认值。**必须有**：`wait_ready` / `_check` 会读这几个字段，
    #: 而验证脚本里的桩对象（`verify_thread_attach.StubSession`）只覆盖
    #: `init()`、不调 `super().__init__()` —— 只在 `__init__` 里赋值的话，
    #: 桩对象上会 `AttributeError`，把「桩写得简单」变成「产品代码坏了」。
    #: 放在类上，两条路径都读得到。
    _wedged: bool = False
    _wedged_reason: str = ""
    _call_timeout: float = CALL_TIMEOUT

    def __init__(self, pid: int, session, script, name: str = "",
                 spawned: bool = False):
        self.pid = pid
        self.name = name
        self.spawned = spawned
        self._session = session
        self._script = script
        self._alive = True
        self._lock = threading.RLock()
        #: 串行化探针 RPC。**与 `_lock` 分开**是有意的：`_lock` 保护的是
        #: 本对象的字段，而这个是「同一时刻只允许一次 Frida 调用」。
        #: 卡死时它会被那个永不返回的线程一直持有 —— 这正是我们检测卡死的
        #: 手段（下一个调用者 acquire 超时就知道探针已经不响应了）。
        self._rpc_lock = threading.Lock()
        #: 探针已判定卡死。一旦置位，后续调用**立刻**失败而不是再等 30 秒
        #: （卡死通常是不可逆的：Frida 的 agent 线程已经不进事件循环了）。
        self._wedged = False
        self._wedged_reason = ""
        self._call_timeout = CALL_TIMEOUT
        # Hook 事件队列：容量放大，满时丢最旧，绝不阻塞游戏线程
        self._events: queue.Queue = queue.Queue(maxsize=20000)
        self._dropped = 0
        self._init_result: dict | None = None
        self._created_at = time.time()
        #: `close()` 里释放引擎域失败时的原因（仅记录，不算关闭失败）
        self._domain_release_error: str | None = None

        script.on("message", self._on_message)
        script.on("destroyed", self._on_destroyed)

    # -------------------------------------------------- 内部回调

    def _on_message(self, message: dict, data) -> None:
        """处理探针发来的消息。必须极快返回，不能阻塞。"""
        try:
            mtype = message.get("type")
            if mtype == "send":
                payload = message.get("payload")
                if isinstance(payload, dict) and payload.get("type") == "hook":
                    self._push_event(payload)
                elif isinstance(payload, dict):
                    # 非 Hook 的主动消息（如 probe_ready）单独记录
                    self._push_event({"type": "notice", "payload": payload})
                else:
                    self._push_event({"type": "notice", "payload": payload})
            elif mtype == "error":
                self._push_event({
                    "type": "script_error",
                    "description": message.get("description"),
                    "stack": message.get("stack"),
                    "lineNumber": message.get("lineNumber"),
                    "fileName": message.get("fileName"),
                })
            elif mtype == "log":
                # 探针侧的 console.log。以前只往 self._console 里塞，
                # 而那个列表**没有任何读取方**（唯一的 console_output() 也没有
                # MCP 出口），于是探针日志写进去就消失 —— 排查注入问题时
                # 最想看的东西恰好看不到。
                # 现改为入事件队列，drain_events 就能取到（契约 §3：能力必须有出口）。
                self._push_event({
                    "type": "log",
                    "level": message.get("level", "info"),
                    "payload": message.get("payload"),
                })
        except Exception as exc:  # 回调里绝不能抛，否则会污染 Frida 线程
            warn(f"处理探针消息失败：{exc}")

    def _on_destroyed(self) -> None:
        self._alive = False

    def _push_event(self, ev: dict) -> None:
        """入队；队列满时丢最旧的一条并计数（保证不阻塞）。"""
        try:
            self._events.put_nowait(ev)
        except queue.Full:
            try:
                self._events.get_nowait()
                self._events.put_nowait(ev)
            except queue.Empty:
                pass
            self._dropped += 1

    # -------------------------------------------------- 状态

    @property
    def alive(self) -> bool:
        return self._alive

    def _check(self) -> None:
        if not self._alive:
            raise SessionError(
                f"会话已失效（pid={self.pid} 可能已退出或被 detach），请重新注入"
            )
        if self._wedged:
            raise SessionError(
                f"探针已判定卡死（pid={self.pid}）：{self._wedged_reason}。"
                "后续调用会立刻失败而不是再等超时 —— 请 close_session() 后重新注入。"
            )

    @property
    def wedged(self) -> bool:
        """探针是否已被判定卡死（一次 RPC 超过 `CALL_TIMEOUT` 没返回）。"""
        return self._wedged

    def _call(self, method: str, *args) -> Any:
        """调用探针的 rpc.exports，**带超时**。

        为什么不能直接 `self._script.exports_sync.x(...)`（这是缺陷 #2 的修复）：
        Frida 的同步 RPC **没有超时参数**。一次不返回的调用会永久挂住调用方，
        而且因为工具是同步函数（见 `server.py` 的说明），它会连带把整个
        MCP 事件循环占死 —— 实测 `launch_game` 卡 11 分钟、期间任何工具都不回话。

        做法：把这次调用交给一个**看守线程**执行，主线程只 `join(timeout)`。
        超时就如实报错并把会话标记为 wedged（后续调用立刻失败，不再各等 30 秒）。

        **不假装调用被取消了**：Frida 侧那个线程仍会卡在那里，所以我们
        · 不释放 `_rpc_lock`（由超时后的新线程去 acquire 时超时发现），
        · 把会话标 wedged，让调用方去 `close_session` 重新注入 ——
        唯一诚实的出路。返回 `ok:false` 而不是伪装成一次成功的空调用。
        """
        self._check()
        with self._lock:
            timeout = self._call_timeout
        # 先抢 RPC 锁：若上一次调用卡住不放，这里就会超时 ——
        # 于是「探针不响应」在**第二次**调用时只需等 timeout 而不是永久。
        acquired = self._rpc_lock.acquire(timeout=timeout)
        if not acquired:
            self._mark_wedged(f"{method} 等待前一次调用释放锁超时（{timeout:g}s）")
            raise SessionError(self._wedged_reason)

        box: dict[str, Any] = {}
        done = threading.Event()

        def _invoke() -> None:
            try:
                box["value"] = getattr(self._script.exports_sync, method)(*args)
            except Exception as exc:  # noqa: BLE001 - 原样带回主线程再判
                box["error"] = exc
            finally:
                done.set()

        th = threading.Thread(target=_invoke, daemon=True,
                              name=f"gamemod-rpc-{method}")
        th.start()
        finished = done.wait(timeout)
        if not finished:
            # 超时：**不**释放 _rpc_lock（那个线程还持有它）。
            self._mark_wedged(f"{method} 超过 {timeout:g}s 没有返回")
            raise SessionError(
                f"调用探针 {method} 超时（{timeout:g}s 未返回）。"
                "探针线程很可能卡在 IL2CPP/Mono 内部（实测现场是游戏进入 "
                "`Fatal error in GC`）。会话已标记为不可用：请 close_session() "
                "后重新注入；游戏本身可能需要重启。"
            )
        try:
            self._rpc_lock.release()
        except RuntimeError:  # pragma: no cover - 不该发生
            pass

        if "error" in box:
            exc = box["error"]
            # 会话在调用过程中失效时，如实报告而不是伪装成功
            if not self._alive:
                raise SessionError(
                    f"调用 {method} 时会话失效（进程可能已退出）"
                ) from exc
            raise SessionError(f"调用探针 {method} 失败：{exc}") from exc
        return box.get("value")

    def _mark_wedged(self, reason: str) -> None:
        """标记探针卡死，并留痕（这是需要人看见的事件）。"""
        self._wedged = True
        self._wedged_reason = reason
        warn(f"探针卡死 pid={self.pid}：{reason}")

    def _bounded(self, fn: Any, what: str) -> Any:
        """在有界时间内执行一个**没有超时**的 Frida 同步调用。

        `script.unload()` 与 `session.detach()` 也是同步 Frida 调用，
        同样可能永不返回 —— 实测探针卡死时它们会把收尾一起挂住
        （复现脚本因此挂了 18 分钟）。所以收尾步骤也必须走这里。

        超时**不假装成功**：抛出 `SessionError`，由 `close()` 如实记进
        `steps` 与 `errors`，让调用方知道还剩什么没收拾。
        """
        box: dict[str, Any] = {}
        done = threading.Event()

        def _run() -> None:
            try:
                box["value"] = fn()
            except Exception as exc:  # noqa: BLE001
                box["error"] = exc
            finally:
                done.set()

        th = threading.Thread(target=_run, daemon=True, name=f"gamemod-{what}")
        th.start()
        if not done.wait(self._call_timeout):
            raise SessionError(
                f"{what} 超过 {self._call_timeout:g}s 没有返回"
                "（探针卡死时 Frida 的同步调用同样会挂住）")
        if "error" in box:
            raise box["error"]
        return box.get("value")

    # -------------------------------------------------- 初始化

    def init(self, preferred_module: str = "") -> dict:
        """初始化探针（解析模块与导出）。

        **只缓存成功结果**。这一点很关键：
        spawn 模式下进程刚 resume，GameAssembly.dll 还没被加载，
        此时 init 必然失败。如果把这个失败也缓存住，
        之后无论等多久都会一直返回「未找到 IL2CPP 运行时模块」，
        而实际上游戏加载完就好了。
        """
        if self._init_result is not None and self._init_result.get("ok"):
            return self._init_result
        res = self._call("init", preferred_module or None)
        if res.get("ok"):
            self._init_result = res
        return res

    def wait_ready(self, timeout: float = 30.0,
                   preferred_module: str = "",
                   interval: float = 0.5,
                   settle: float | None = None) -> dict:
        """轮询直到 IL2CPP 运行时可用（或超时）。

        spawn 之后必须用它而不是一次性 init：
        DLL 加载 + il2cpp 初始化需要时间，抢跑只会拿到「模块未找到」。
        所以**不能**把「模块未找到」当成「没救了」—— 游戏刚起来时
        `GameAssembly.dll` 往往还没加载完（实测刚 spawn 完第一次 init
        就是模块未找到）。等待预算该给多少由调用方按引擎决定：
        IL2CPP 给 60 秒，Mono/UE 游戏永远不会有这个模块，给 2 秒就够
        （见 `engine/state.py` 的 `ready_timeout_for`）。

        `settle` 是**第一次探测前**的静默秒数（默认 `READY_SETTLE_DELAY`）。
        为什么要它：对照实验显示「游戏启动头几秒就去探测」会显著提高
        游戏进 `Fatal error in GC` 的概率（0 秒静默 + 紧循环 = 8/8 卡死，
        3 秒静默 + 紧循环 = 0/8）。这是一条**缓解**，不是根因修复 ——
        依据与边界见 `READY_SETTLE_DELAY` 的注释。

        这个静默**不占用** `timeout` 预算：预算管的是「运行时要多久才就绪」，
        而静默是「先别去碰它」。两者混在一起会让「等了 3 秒才第一次探测」
        被记成「运行时 3 秒才就绪」，把观测值搞脏。
        """
        if settle is None:
            settle = READY_SETTLE_DELAY
        if settle > 0:
            time.sleep(settle)
        deadline = time.time() + max(0.0, timeout)
        last: dict = {"ok": False, "error": "尚未尝试初始化"}
        attempt = 0
        while True:
            attempt += 1
            try:
                last = self.init(preferred_module)
            except Exception as exc:
                last = {"ok": False, "error": str(exc)}
            if last.get("ok"):
                last = dict(last)
                last["attempts"] = attempt
                last["waited"] = round(max(0.0, timeout - (deadline - time.time())), 2)
                return last
            if self._wedged:
                # 探针已经卡死：再轮询下去每次都会立刻失败，纯属空转。
                # 立刻返回并**说清是什么**，别让调用方以为是「运行时没起来」。
                out = dict(last)
                out["attempts"] = attempt
                out["timed_out"] = True
                out["wedged"] = True
                out["waited"] = round(max(0.0, timeout - (deadline - time.time())), 2)
                return out
            if time.time() >= deadline:
                out = dict(last)
                out["attempts"] = attempt
                out["timed_out"] = True
                out["waited"] = round(timeout, 2)
                return out
            time.sleep(interval)

    def ping(self) -> dict:
        return self._call("ping")

    # -------------------------------------------------- 查询能力

    def runtime_info(self, preferred_module: str = "") -> dict:
        return self._call("runtime_info", preferred_module or None)

    def list_images(self, preferred_module: str = "") -> dict:
        return self._call("list_images", preferred_module or None)

    def class_info(self, namespace: str = "", name: str = "",
                   image: str = "", fields: bool = True,
                   methods: bool = True, method_filter: str = "",
                   preferred_module: str = "") -> dict:
        opts = {
            "namespace": namespace, "name": name, "image": image,
            "fields": fields, "methods": methods,
        }
        if method_filter:
            opts["method_filter"] = method_filter
        return self._call("class_info", opts, preferred_module or None)

    def method_target(self, namespace: str, name: str, method: str,
                      image: str = "", preferred_module: str = "") -> dict:
        return self._call(
            "method_target",
            {"namespace": namespace, "name": name, "method": method, "image": image},
            preferred_module or None,
        )

    def class_fields(self, namespace: str = "", name: str = "", image: str = "",
                     static_only: bool = False, instance_only: bool = False,
                     preferred_module: str = "") -> dict:
        return self._call(
            "class_fields",
            {"namespace": namespace, "name": name, "image": image,
             "static_only": static_only, "instance_only": instance_only},
            preferred_module or None,
        )

    def find_instances(self, namespace: str = "", name: str = "", image: str = "",
                       max_count: int = 100,
                       preferred_module: str = "") -> dict:
        """枚举某个类的活对象实例（拿 instance 指针的正路）。"""
        return self._call(
            "findInstances",
            {"namespace": namespace, "name": name, "image": image,
             "max_count": max_count},
            preferred_module or None,
        )

    # -------------------------------------------------- Hook

    def hook(self, address: str, args: list[str] | None = None,
             label: str = "", max_calls: int = 200,
             backtrace: bool = False, retval: bool = True,
             retval_type: str = "ptr", this_type: str = "",
             preferred_module: str = "") -> dict:
        """在指定地址装 Hook。

        address 可以是绝对地址，也可以由上层用 method_target 得到的 RVA
        加模块基址换算而来。
        """
        spec: dict[str, Any] = {
            "address": address,
            "args": args or [],
            "label": label,
            "max_calls": max_calls,
            "backtrace": backtrace,
            "retval": retval,
            "retval_type": retval_type,
        }
        if this_type:
            spec["this_type"] = this_type
        return self._call("hook", spec, preferred_module or None)

    def hook_method(self, namespace: str, name: str, method: str,
                    image: str = "", args: list[str] | None = None,
                    label: str = "", max_calls: int = 200,
                    backtrace: bool = False, preferred_module: str = "",
                    token: str = "", param_count: int = -1,
                    retval_type: str = "") -> dict:
        """便捷入口：按 类+方法名 直接 Hook。

        这是「元数据优先」工作流的收口：Agent 用静态元数据查到目标方法，
        这里用运行时把类实体化并拿到真实地址，直接装 Hook。

        **重载选择**：同名方法可能有多个重载（token 各不相同）。以前这里
        盲取第一个候选（`usable[0]`），会在不知情的情况下挂到错误的重载上。
        现在必须显式选：

        - 给了 `token`：按 token 精确匹配（最可靠，静态元数据里就有）。
        - 给了 `param_count`：按参数个数匹配；若仍有多个，返回歧义错误。
        - 都没给且候选唯一：直接挂（保持简单场景好用）。
        - 都没给且候选多个：**拒绝猜测**，返回 `ok: False` + 候选清单。

        **返回值类型**：`retval_type` 留空时按运行时给出的 `return_type`
        自动推断（见 `_retval_spec_for`）。这件事必须自动做对，因为
        float / double 返回值在 XMM0 而不在 RAX：按 `ptr` 读会给出一个
        看着像地址、其实毫无意义的整数（实测 `Mathf.Abs(-2.5f)` 得到
        `0x20da0a48360`）。
        """
        tgt = self.method_target(namespace, name, method, image=image,
                                 preferred_module=preferred_module)
        if not tgt.get("ok"):
            return tgt
        cands = tgt.get("candidates") or []
        # 「能不能 Hook」由探针的后端判定并标在候选上（`hookable`）：
        # IL2CPP 的机器码一定在模块内，不在模块内就是可疑候选；
        # Mono 的 JIT 代码**一定**在模块外，只要地址非空就能 Hook。
        # 以前这里写死 `in_module is not False`，于是 Mono 上**所有**方法
        # 都被判成「没有可 Hook 的原生地址」——而 find_hook_target 刚把
        # 同一个候选的地址给出来（实测 Time.get_frameCount 就是如此）。
        usable = [c for c in cands if c.get("hookable") is True]
        if not usable:
            return {
                "ok": False,
                "error": f"方法 {name}.{method} 存在但没有可 Hook 的原生地址"
                         "（可能是抽象/泛型定义或纯转发）",
                "candidates": cands,
            }

        selected, err = _select_overload(usable, token=token,
                                        param_count=param_count,
                                        name=name, method=method)
        if err is not None:
            return err
        chosen = selected
        rv_type = retval_type or _retval_spec_for(chosen.get("return_type"))
        out = self.hook(chosen["pointer"], args=args, label=label or f"{name}.{method}",
                        max_calls=max_calls, backtrace=backtrace,
                        retval_type=rv_type,
                        preferred_module=preferred_module)
        if out.get("ok"):
            out["class"] = tgt.get("class")
            out["method"] = method
            out["chosen"] = {
                "pointer": chosen.get("pointer"),
                "rva_hex": chosen.get("rva_hex"),
                "token": chosen.get("token"),
            }
            out["candidate_count"] = len(cands)
            out["usable_count"] = len(usable)
            # 把「返回值按什么类型读」如实回报：float/double 走 XMM，
            # 读错寄存器会给出看着像地址、其实毫无意义的整数。
            out["retval_type"] = rv_type
            out["return_type"] = chosen.get("return_type")
        return out

    def unhook(self, hook_id: str) -> dict:
        return self._call("unhook", hook_id)

    def unhook_all(self) -> dict:
        return self._call("unhook_all")

    def hook_status(self) -> dict:
        return self._call("hook_status")

    # -------------------------------------------------- 内存

    def read_memory(self, address: str, size: int) -> dict:
        return self._call("read_memory", address, int(size))

    def alloc_buffer(self, size: int) -> dict:
        """分配一块脚本存活期间稳定的缓冲区。

        Frida 的 Memory.alloc 若在 JS 侧无引用会被 GC 回收，跨 RPC 调用
        读取会拿到已复用的内存。本方法的缓冲区由探针持有，适合做
        写入→读回校验，或作为读结构体的临时暂存。
        """
        return self._call("alloc_buffer", int(size))

    def free_buffer(self, address: str) -> dict:
        return self._call("free_buffer", address)

    def list_buffers(self) -> dict:
        return self._call("list_buffers")

    def write_memory(self, address: str, hex_data: str) -> dict:
        return self._call("write_memory", address, hex_data)

    def scan_pattern(self, pattern: str, module: str = "",
                     max_results: int = 32) -> dict:
        # 这里曾有一个 `scope` 参数：传下去放进 opts，但 agent.js 只读
        # `opts.module` / `opts.max_results`，`scope` 从来没被看过一眼 ——
        # 一个「收了参数却什么也不做」的形参比死代码更坏：调用方会以为
        # 扫描范围真的被限制了。限定范围用 `module`。
        opts: dict[str, Any] = {"pattern": pattern, "max_results": max_results}
        if module:
            opts["module"] = module
        return self._call("scan_pattern", opts)

    def eval_js(self, code: str) -> dict:
        return self._call("eval_js", code)

    def gc_control(self, disable: bool) -> dict:
        return self._call("gc_control", bool(disable))

    # -------------------------------------------------- 事件

    def drain_events(self, limit: int = 200, kind: str = "") -> dict:
        """取走缓冲区里的事件。

        模仿 AiMod 的 /v1/events?after= 增量语义：取走即消费，
        调用方保存 last_seq 就能做增量拉取。

        `kind` 过滤的是**整条队列**，不是只看队首 —— 理由见下面的注释。
        """
        out: list[dict] = []
        if not kind:
            while len(out) < limit:
                try:
                    out.append(self._events.get_nowait())
                except queue.Empty:
                    break
        else:
            # 必须把当前队列**整个取空**再筛，不能「看到队首不匹配就停」。
            #
            # 修复前的写法是：
            #     if kind and ev.get("type") != kind:
            #         self._push_event(ev)
            #         break
            # 于是过滤**只看队首那一条**：队首类型不是要的那种时，
            # 后面即使排着一堆匹配事件也一条都取不到。实测在真实游戏上
            # `drain_events(kind="hook")` 返回 0 条而 `pending_approx` 有 196，
            # 去掉过滤立刻能取到 `type:"hook"` 的事件 —— 诱因是注入后队首
            # 恒有一条 `notice`(probe_ready)，正好把整条队列挡住。
            # 这是「静默返回空」，比报错更糟：调用方会以为期间没有任何
            # Hook 触发，从而去排查 Hook 落点（而落点是对的）。
            pending: list[dict] = []
            while True:
                try:
                    pending.append(self._events.get_nowait())
                except queue.Empty:
                    break
            kept: list[dict] = []
            for ev in pending:
                if ev.get("type") == kind and len(out) < limit:
                    out.append(ev)
                else:
                    kept.append(ev)
            # 未取走的事件按**原相对顺序**放回，队列语义（含满时丢最旧）
            # 因此完全不变。超出 limit 的匹配事件同样留在队列里，
            # 下次还能取到 —— 与无 kind 时的行为一致，不偷偷丢数据。
            for ev in kept:
                self._push_event(ev)

        # 统计剩余量（queue.qsize 只是近似，仅用于展示）
        return {
            "ok": True,
            "events": out,
            "returned": len(out),
            "pending_approx": self._events.qsize(),
            "dropped": self._dropped,
        }

    def clear_events(self) -> dict:
        """清空事件队列（丢弃未读取的事件）。

        供 `drain_events` 之外需要「从现在开始只看新事件」的场景；
        `verify_il2cpp_runtime.py` / `verify_runtime.py` 用它保证两段
        观测互不串味。
        """
        n = 0
        while True:
            try:
                self._events.get_nowait()
                n += 1
            except queue.Empty:
                break
        return {"ok": True, "cleared": n}

    # -------------------------------------------------- 关闭

    def close(self) -> dict:
        """卸载脚本并断开。Hook 会被一并卸掉（进程恢复原状）。

        **不许假成功**：早期实现把卸载 Hook / `script.unload()` / `session.detach()`
        三步各包一个 `except Exception: pass`，然后无论如何都返回
        `restored: True`。实测三步**全部**抛异常时，返回值依然是
        `{"ok": True, "restored": True}` —— 而 `close_session` 的语义就是
        「把游戏恢复原状」，Agent 会据此结束任务并宣称收尾干净，
        实际上 Hook 可能仍挂在进程里（游戏行为仍被篡改）。

        更糟的是失败后还会 `_alive = False` 并把自己从 `_sessions` 摘掉，
        于是**再也无法补救**（后续 unhook / close_session 都报「没有已注入的运行时会话」）。

        所以现在逐步收集失败原因：任一步失败就 `ok: False` + `restored: False`，
        并把已经做到哪一步如实列出，让调用方知道还剩什么没收拾。
        """
        if not self._alive:
            return {"ok": True, "already_closed": True, "pid": self.pid}
        removed = 0
        errors: list[str] = []
        steps: dict[str, bool] = {}
        #: 探针卡死时不再逐项尝试 —— 见下方 `wedged` 分支。
        #: 实测（docs/重启验证与运行时理解报告.md §4.2）：探针 RPC 不返回时，
        #: `script.unload()` / `session.detach()` **同样**是没有超时的同步
        #: Frida 调用，于是连收尾都会挂住（我的复现脚本因此挂了 18 分钟）。
        #: 所以卡死路径必须**跳过**这些同步调用，只做有界的收尾。
        wedged = self._wedged
        if wedged:
            steps["skipped_wedged"] = True
            self._alive = False
            with _sessions_lock:
                if _sessions.get(self.pid) is self:
                    del _sessions[self.pid]
            # **ok 必须是 False**：本工具的语义是「游戏已恢复原状」，
            # 而卡死时我们**没能**卸载 Hook / 摘域 —— 游戏进程里可能仍留着
            # 探针与 Hook。报 ok:True 就是 AGENTS.md 禁止的假成功
            # （`close_session` 的 docstring 专门写了这一条）。
            #
            # 但 `session_released` 是 True：这个会话对象确实已经作废、
            # 登记也已摘掉，调用方**可以重新注入**。两件事必须分开表达 ——
            # 早先这里只给了 `restored: False` 却把 `ok` 留成 True，
            # 结果 `close_session` 顶层报出 `restored: true`，
            # 与子对象里的 `restored: false` 自相矛盾（实测发现）。
            return {
                "ok": False,
                "pid": self.pid,
                "hooks_removed": 0,
                "restored": False,
                #: 会话对象已作废且已注销 —— 调用方可以重新注入，
                #: 但这**不等于**游戏已恢复原状。
                "session_released": True,
                "wedged": True,
                "steps": steps,
                "error": (
                    f"探针已卡死（{self._wedged_reason}），无法卸载 Hook / "
                    "摘除引擎域 —— 游戏**可能未被恢复原状**。"
                ),
                "warning": (
                    f"探针已卡死（{self._wedged_reason}），无法执行卸载 Hook / "
                    "摘除引擎域 / 卸载脚本这些同步收尾步骤 —— 它们同样会挂住。"
                    "已断开本会话的登记（可以重新注入），但**游戏进程内可能仍留有"
                    "探针与 Hook**，请直接结束该游戏进程（taskkill / 重启游戏）后"
                    "再重新注入。"
                ),
            }
        try:
            st = self.hook_status()
            removed = st.get("count", 0)
            self.unhook_all()
            steps["hooks_removed"] = True
        except Exception as exc:
            steps["hooks_removed"] = False
            errors.append(f"卸载 Hook 失败：{exc}")
        # 关键顺序：**必须在 script.unload() 之前**把探针线程从引擎域里摘出去。
        # 不摘的话，IL2CPP 域里会留下指向即将被释放的 agent 内存的线程记录，
        # 游戏会在随后的 GC 中崩（实测三款 IL2CPP 游戏 detach 后 80~160s
        # 崩在 frida-agent.dll_unloaded / 0xc0000005）。详见 agent.js 的 releaseDomain。
        try:
            # 走 `_call` 而不是直接 `exports_sync.dispose()`：同样是 Frida 同步
            # RPC，同样可能不返回（探针卡死时最需要收尾，恰恰最可能挂住）。
            # 经 `_call` 至少有超时，失败也如实记进 `steps`。
            self._call("dispose")
            steps["domain_released"] = True
        except Exception as exc:
            # 探针不支持 dispose（旧版 agent.js）不该让整个关闭失败：
            # 记下来但不计入 errors，只在返回里如实标注。
            steps["domain_released"] = False
            self._domain_release_error = str(exc)
        try:
            self._bounded(lambda: self._script.unload(), "卸载脚本")
            steps["script_unloaded"] = True
        except Exception as exc:
            steps["script_unloaded"] = False
            errors.append(f"卸载脚本失败：{exc}")
        try:
            self._bounded(lambda: self._session.detach(), "断开会话")
            steps["detached"] = True
        except Exception as exc:
            steps["detached"] = False
            errors.append(f"断开会话失败：{exc}")

        restored = not errors

        if restored:
            # 只有全部成功才注销会话：失败时保留注册，调用方还能重试
            self._alive = False
            with _sessions_lock:
                if _sessions.get(self.pid) is self:
                    del _sessions[self.pid]
            progress(f"已关闭运行时会话 pid={self.pid}（卸载 Hook {removed} 个）")
            out = {"ok": True, "pid": self.pid, "hooks_removed": removed,
                   "restored": True, "steps": steps}
            if self._domain_release_error is not None:
                # 摘域失败 = 游戏仍可能在随后 GC 中崩，必须如实说，不能假装没事
                out["warning"] = (
                    "探针线程未能从引擎域摘除（"
                    + self._domain_release_error
                    + "）；游戏可能在随后的垃圾回收中崩溃，"
                    "建议观察一两分钟或直接重启游戏。"
                )
            return out

        warn(f"关闭运行时会话 pid={self.pid} 未完全成功：{'；'.join(errors)}")
        return {
            "ok": False,
            "pid": self.pid,
            "hooks_removed": removed,
            "restored": False,
            "error": "；".join(errors) + "；游戏可能未恢复原状，可重试 close_session。",
            "steps": steps,
        }

    def __repr__(self) -> str:
        state = "alive" if self._alive else "dead"
        return f"<RuntimeSession pid={self.pid} {self.name!r} {state}>"


# ---------------------------------------------------------------- 注入入口


def attach(pid: int, name: str = "", timeout: float = 10.0,
           init: bool = True, preferred_module: str = "") -> RuntimeSession:
    """注入指定进程并加载探针。

    同一 pid 重复调用会复用已有会话（不叠加注入）。
    """
    frida = _load_frida()

    with _sessions_lock:
        existing = _sessions.get(pid)
        if existing is not None and existing.alive:
            return existing
        if existing is not None:
            # 陈旧会话：清理掉再重来
            del _sessions[pid]

    try:
        device = frida.get_local_device()
    except Exception as exc:
        raise RuntimeUnavailable(f"无法访问本地 Frida 设备：{exc}") from exc

    try:
        session = device.attach(pid)
    except Exception as exc:
        # ⚠️ 这里**不再**写「需以管理员身份运行」—— 实测（2026-10）
        # 普通用户权限就能注入游戏进程：本 python 与游戏进程的完整性
        # 级别都是 `Medium`，attach → runtime_info → find_instances 全成功。
        # 真正的失败条件是**权限级不匹配**：游戏以管理员身份运行时
        # 完整性级别是 `High`，普通权限的 Agent 才注入不进去。
        # 一句话：**同级别就行，不是「必须管理员」**。
        raise SessionError(
            f"注入 pid={pid} 失败：{exc}。常见原因：进程已退出、"
            "**权限级不匹配**（游戏以管理员身份运行、而本进程不是；"
            "普通权限的游戏用普通权限即可注入，不需要管理员）、"
            "或目标游戏带反调试 / 反注入保护。"
        ) from exc

    # 等待 session 就绪（某些情况下 attach 后需要短暂等待）
    try:
        session.enable_child_gating()
    except Exception:
        pass  # 非必需能力，失败不影响

    try:
        script = session.create_script(agent_source(), name="gamemod-probe")
    except Exception as exc:
        try:
            session.detach()
        except Exception:
            pass
        raise SessionError(f"创建探针脚本失败：{exc}") from exc

    rs = RuntimeSession(pid, session, script, name=name)

    try:
        script.load()
    except Exception as exc:
        try:
            session.detach()
        except Exception:
            pass
        raise SessionError(
            f"加载探针脚本失败：{exc}。可能是进程架构与 Frida 不匹配，"
            "或进程处于不可注入状态。"
        ) from exc

    with _sessions_lock:
        _sessions[pid] = rs

    progress(f"已注入 pid={pid}" + (f" ({name})" if name else ""))

    if init:
        # attach 已运行的进程时模块通常已加载，但游戏可能仍在启动中，
        # 所以同样用轮询而非一次性判断。
        res = rs.wait_ready(timeout=20.0, preferred_module=preferred_module)
        if not res.get("ok"):
            # 注入成功但 il2cpp 初始化失败：保留会话（可能只是引擎不匹配），
            # 由上层决定是否继续；这里把原因如实带出来
            warn(f"探针初始化未成功：{res.get('error')}")

    return rs


#: spawn 子进程时的 stdio 策略。**默认的 `inherit` 会破坏 MCP 协议**：
#: 本服务器用 stdout 跑 JSON-RPC，而 `launch_game` 启动的 Unity 游戏会往
#: 同一个 stdout 打日志 —— 实测 Garden of Witches 一启动就灌进 30 行，
#: 头两行是 `[UnityMemory] Configuration Parameters - Can be set up in
#: boot.config` 与 `"memorysetup-bucket-allocator-granularity=16"`。
#: 后者是**合法 JSON 字符串**，客户端 `json.loads` 会成功，然后把一个 `str`
#: 当 JSON-RPC 消息用 —— 实测把客户端的
#: `AttributeError: 'str' object has no attribute 'get'` 伪装成脚本自身 bug。
#: 换成 `pipe` 后子进程输出由 Frida 自己接住（实测 4 MB 洪水也不会让子进程
#: 卡在 write 上），再经 device 的 `output` 信号转发到 stderr。
#: 注意 Frida 17 的 `FridaStdio` 只有 `inherit` / `pipe` 两个值，**没有** `ignore`。
_SPAWN_STDIO = "pipe"

#: 子进程输出转发只注册一次（device 级信号）
_output_hooked = False
_output_lock = threading.Lock()


def _hook_child_output(device) -> None:
    """把 spawn 出来的子进程输出转发到 **stderr**。

    不做这件事的话，`stdio="pipe"` 会让游戏的日志彻底消失（被 Frida 读走后
    丢掉）—— 那对排障是净损失。转发到 stderr 既保住了信息，又不碰协议通道。
    """
    global _output_hooked
    with _output_lock:
        if _output_hooked:
            return
        try:
            def _on_output(pid, fd, data) -> None:
                try:
                    text = (data.decode("utf-8", "replace")
                            if isinstance(data, (bytes, bytearray)) else str(data))
                    text = text.rstrip()
                    if text:
                        debug(f"[子进程 {pid} fd={fd}] {text}")
                except Exception:  # noqa: BLE001 - 回调里绝不能抛
                    pass

            device.on("output", _on_output)
            _output_hooked = True
        except Exception as exc:  # noqa: BLE001 - 转发失败不该挡住注入
            warn(f"无法转发子进程输出（不影响注入，但游戏日志会丢失）：{exc}")


def _spawn_detached_stdout(device, argv: list[str], exe: str,
                           cwd: str) -> int:
    """spawn 游戏，并把它的 stdout 从我们的协议通道上摘下来。"""
    try:
        return device.spawn(argv, cwd=cwd or None, stdio=_SPAWN_STDIO)
    except (TypeError, ValueError) as exc:
        # 只可能是「这个 Frida 版本不认 stdio 参数 / 不认 pipe」，
        # 不是游戏本身起不来。退化为继承，但把风险**说出来**。
        warn(f"当前 Frida 不支持 spawn(stdio={_SPAWN_STDIO!r})：{exc}；"
             "游戏输出将与 MCP 的 stdout 混流，可能污染 JSON-RPC")
    except Exception as exc:
        raise SessionError(
            f"启动 {exe} 失败：{exc}。请确认路径正确且可执行。"
        ) from exc
    try:
        return device.spawn(argv, cwd=cwd or None)
    except Exception as exc:
        raise SessionError(
            f"启动 {exe} 失败：{exc}。请确认路径正确且可执行。"
        ) from exc


def spawn_and_attach(exe: str, args: list[str] | None = None,
                     cwd: str = "", timeout: float = 20.0,
                     init: bool = True,
                     preferred_module: str = "") -> RuntimeSession:
    """启动游戏并在一开始就注入（能在游戏初始化前装 Hook）。

    这对「抢在对象创建之前挂钩」很关键 —— 比 attach 已经跑起来的进程强。
    """
    frida = _load_frida()
    try:
        device = frida.get_local_device()
    except Exception as exc:
        raise RuntimeUnavailable(f"无法访问本地 Frida 设备：{exc}") from exc

    argv = [exe] + list(args or [])
    _hook_child_output(device)
    pid = _spawn_detached_stdout(device, argv, exe, cwd)

    try:
        session = device.attach(pid)
        script = session.create_script(agent_source(), name="gamemod-probe")
    except Exception as exc:
        try:
            device.kill(pid)
        except Exception:
            pass
        raise SessionError(f"对新建进程注入失败：{exc}") from exc

    rs = RuntimeSession(pid, session, script, name=os.path.basename(exe),
                        spawned=True)
    try:
        script.load()
    except Exception as exc:
        try:
            device.kill(pid)
        except Exception:
            pass
        raise SessionError(f"加载探针脚本失败：{exc}") from exc

    with _sessions_lock:
        _sessions[pid] = rs

    # 恢复进程运行（spawn 后默认是挂起的）
    try:
        device.resume(pid)
    except Exception as exc:
        warn(f"恢复进程运行失败：{exc}")

    progress(f"已启动并注入 {exe} (pid={pid})")
    if init:
        # 注意：这里必须**等**，不能只 init 一次。
        # spawn 后进程刚 resume，GameAssembly.dll 尚未加载，
        # 一次性 init 会拿到「模块未找到」的假失败。
        # 失败也不抛错 —— 有些目标本来就不是 IL2CPP，
        # 由调用方决定是否继续（AOB/Hook 不需要 IL2CPP）。
        rs.wait_ready(timeout=30.0, preferred_module=preferred_module)
    return rs


def get_session(pid: int) -> RuntimeSession | None:
    """取已有会话；不存在或已失效返回 None。"""
    with _sessions_lock:
        rs = _sessions.get(pid)
        if rs is not None and not rs.alive:
            del _sessions[pid]
            return None
        return rs


def list_sessions() -> list[dict]:
    """列出当前活跃会话。"""
    with _sessions_lock:
        out = []
        for pid, rs in list(_sessions.items()):
            if not rs.alive:
                del _sessions[pid]
                continue
            out.append({
                "pid": pid,
                "name": rs.name,
                "spawned": rs.spawned,
                "created_at": rs._created_at,
                "init": (rs._init_result or {}).get("ok"),
                "module": (rs._init_result or {}).get("module"),
                "pending_events": rs._events.qsize(),
            })
        return out


def close_session(pid: int) -> dict:
    with _sessions_lock:
        rs = _sessions.get(pid)
    if rs is None:
        return {"ok": False, "error": f"没有 pid={pid} 的活跃会话"}
    return rs.close()


def close_all_sessions() -> dict:
    with _sessions_lock:
        pids = list(_sessions)
    closed = []
    for pid in pids:
        try:
            close_session(pid)
            closed.append(pid)
        except Exception as exc:
            warn(f"关闭 pid={pid} 会话失败：{exc}")
    return {"ok": True, "closed": closed, "count": len(closed)}
