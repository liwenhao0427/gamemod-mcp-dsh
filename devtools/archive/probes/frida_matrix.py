#!/usr/bin/env python3
"""隔离实验：Frida 的 spawn / stdio 策略 / 探针脚本，谁把游戏卡住。

背景（实测）：同一个 `ved.exe`
  · 手动双击启动        → 约 10~20 秒进主窗口（标题 `VED`）
  · `launch_game` 启动  → 长期停在 BepInEx preloader（约 650 MB），进不去主场景

所以必须把四件事**分开**看，否则会把「Frida 的 spawn」误判成「探针把游戏打崩了」：
  A. spawn（挂起）+ attach + resume，**不加载** agent.js，stdio=inherit
  B. spawn（挂起）+ attach + resume，**不加载** agent.js，stdio=pipe
  C. spawn（挂起）+ attach + 加载 agent.js，stdio=pipe（≈ launch_game 的注入部分）
  D. 对**手动启动好**的进程 attach + 加载 agent.js（探针本身的影响）

判据用**窗口标题**（`BepInEx ... - ved` → `VED`）与工作集，不靠猜。

用法：
    python .cache-test\frida_matrix.py A|B|C <exe>
    python .cache-test\frida_matrix.py D <exe> <pid>
"""

from __future__ import annotations

import ctypes
import sys
import threading
import time

import frida

OBSERVE_SECONDS = int(__import__("os").environ.get("OBSERVE", "90"))
AGENT_PATH = r"E:\project\GameModMCP\src\gamemod_mcp\runtime\agent.js"

_u32 = ctypes.windll.user32


def window_title(pid: int) -> str:
    """通过 EnumWindows 找该 pid 的可见窗口标题。

    为什么不用 psutil：本机已装 psutil 与否不确定，而 AGENTS.md 要求运行期
    只依赖 mcp + frida；测试脚本同理，避免为了读一个标题引入新依赖。
    """
    found: list[str] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(hwnd, _lparam):
        wpid = ctypes.c_ulong()
        _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value == pid:
            n = _u32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                _u32.GetWindowTextW(hwnd, buf, n + 1)
                if buf.value.strip():
                    found.append(buf.value)
        return True

    _u32.EnumWindows(cb, None)
    return found[0] if found else ""


def rss_mb(pid: int) -> float:
    """读进程工作集（MB）。用 kernel32 而不是第三方库。"""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = ctypes.windll.kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return -1.0
    try:
        class PMC(ctypes.Structure):
            _fields_ = [("cb", ctypes.c_ulong),
                        ("PageFaultCount", ctypes.c_ulong),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]
        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        if not ctypes.windll.psapi.GetProcessMemoryInfo(
                h, ctypes.byref(pmc), pmc.cb):
            return -1.0
        return pmc.WorkingSetSize / 1048576.0
    finally:
        ctypes.windll.kernel32.CloseHandle(h)


def observe(pid: int, label: str, stop_when_title: str = "VED") -> str:
    """观察进程；返回最终看到的标题。"""
    t0 = time.time()
    last = ""
    while time.time() - t0 < OBSERVE_SECONDS:
        time.sleep(4)
        mb = rss_mb(pid)
        if mb < 0:
            print(f"[{label}] t+{time.time()-t0:.0f}s **进程已消失**")
            return "<gone>"
        title = window_title(pid)
        if title != last:
            print(f"[{label}] t+{time.time()-t0:.0f}s WS={mb:.0f}MB "
                  f"title={title!r}")
            last = title
        if stop_when_title and stop_when_title in title:
            print(f"[{label}] t+{time.time()-t0:.0f}s 到达目标标题 {title!r}")
            return title
    print(f"[{label}] 观察结束（{OBSERVE_SECONDS}s），最后标题 {last!r}")
    return last


def main() -> int:
    mode = sys.argv[1].upper()
    exe = sys.argv[2]
    dev = frida.get_local_device()
    agent = open(AGENT_PATH, encoding="utf-8").read()

    if mode == "D":
        pid = int(sys.argv[3])
        print(f"[D] 对已在运行的 pid={pid} attach，标题={window_title(pid)!r}")
        sess = dev.attach(pid)
        script = sess.create_script(agent, name="gamemod-probe")
        script.load()
        print("[D] 探针已加载")
        t0 = time.time()
        r = script.exports_sync.init(None)
        print(f"[D] init({time.time()-t0:.2f}s) -> ok={r.get('ok')} "
              f"module={r.get('module')} error={str(r.get('error'))[:200]}")
        observe(pid, "D-attach-agent")
        script.unload()
        sess.detach()
        return 0

    stdio = "pipe" if mode in ("B", "C", "E", "F", "G") else "inherit"
    load_agent = mode in ("C", "E", "F", "G")
    label = { "A": "A-spawn-inherit-noagent",
              "B": "B-spawn-pipe-noagent",
              "C": "C-spawn-pipe-agent-suspended",
              "E": "E-spawn-pipe-agent-resumed",
              "F": "F-spawn-pipe-agent-delayed",
              "G": "G-spawn-resume-verbose-init" }[mode]
    verbose_init = mode == "G"

    # E 是 launch_game 的**真实顺序**：先 resume 再轮询 init。
    # F 与 E 只差一件事：resume 之后**先等游戏自己跑起来**，再加载探针。
    # 若 F 能进主场景而 E 不能，就说明「过早 il2cpp_thread_attach」是根因。
    # G 与 E 相同，只是把**每一次** init 的返回逐条打出来（要证据，不要结论）。
    resume_first = mode in ("E", "F", "G")
    delay_load = float(__import__("os").environ.get("DELAY", "25")) \
        if mode == "F" else 0.0
    if mode in ("E", "F"):
        def _on_output(pid, fd, data):
            try:
                text = data.decode("utf-8", "replace") if isinstance(
                    data, (bytes, bytearray)) else str(data)
                for line in text.splitlines():
                    if line.strip():
                        print(f"[E][game fd={fd}] {line.strip()}")
            except Exception:
                pass
        dev.on("output", _on_output)

    t0 = time.time()
    pid = dev.spawn([exe], cwd=r"E:\project\Ved", stdio=stdio)
    print(f"[{label}] spawn(stdio={stdio}) 用了 {time.time()-t0:.2f}s pid={pid}")
    sess = dev.attach(pid)
    script = None
    if resume_first:
        dev.resume(pid)
        print(f"[{label}] resume 完成（先 resume 再等就绪）t+{time.time()-t0:.1f}s")
    if delay_load:
        print(f"[{label}] 先等游戏自己跑 {delay_load:.0f}s 再加载探针")
        time.sleep(delay_load)
        print(f"[{label}] 加载前标题={window_title(pid)!r} "
              f"WS={rss_mb(pid):.0f}MB")
    if load_agent:
        script = sess.create_script(agent, name="gamemod-probe")
        script.load()
        print(f"[{label}] 探针已加载 t+{time.time()-t0:.1f}s")
        # 带上限地等就绪。硬上限 60 秒 —— 与 ensure_runtime 的预算一致，
        # 但本次实验感兴趣的是「游戏本身还能不能起来」。
        deadline = time.time() + 60
        attempt = 0
        while time.time() < deadline:
            attempt += 1
            t_call = time.time()
            # 每次 init 都用**看门狗线程**计时：Frida 的同步 RPC 没有超时，
            # 一次不返回的调用会把 wait_ready 的整个时间预算架空 ——
            # 这正是「launch_game 永不返回」的形态，必须能看见。
            done = []

            def _call_init():
                try:
                    done.append(script.exports_sync.init(None))
                except Exception as e:  # noqa: BLE001
                    done.append({"ok": False,
                                 "error": f"{type(e).__name__}: {e}"})

            th = threading.Thread(target=_call_init, daemon=True)
            th.start()
            th.join(timeout=20)
            if th.is_alive():
                print(f"[{label}]   init#{attempt} **超过 20 秒未返回**"
                      f"（Frida 同步 RPC 无超时；wait_ready 的预算被架空）")
                # 继续等，看它到底会不会回来
                th.join(timeout=120)
                print(f"[{label}]   init#{attempt} 再等 120 秒后 "
                      f"{'仍未返回' if th.is_alive() else '返回了'}")
                break
            r = done[0] if done else {"ok": False, "error": "无返回"}
            if verbose_init:
                print(f"[{label}]   init#{attempt} t+{time.time()-t0:.1f}s "
                      f"耗时{time.time()-t_call:.2f}s ok={r.get('ok')} "
                      f"err={str(r.get('error'))[:120]!r}")
            if r.get("ok"):
                print(f"[{label}] init 就绪 t+{time.time()-t0:.1f}s "
                      f"attempt={attempt}")
                break
            time.sleep(0.5)
        else:
            print(f"[{label}] init 60 秒未就绪（继续观察游戏本身）")
    if not resume_first:
        dev.resume(pid)
        print(f"[{label}] resume 完成 t+{time.time()-t0:.1f}s")
    observe(pid, label)

    try:
        if script is not None:
            script.unload()
        sess.detach()
    except Exception as exc:
        print(f"[{label}] 收尾失败: {exc}")
    return 0


if __name__ == "__main__":
    # 输出**同时**落盘：`Select-Object -Last N` 会缓冲到 EOF，
    # 而本实验的失败形态恰恰是「进程不退出」—— 那种情况下管道里什么都看不到。
    _log = open(
        __import__("os").environ.get(
            "LOG", r"E:\project\GameModMCP\.cache-test\matrix.log"),
        "a", encoding="utf-8")

    class _Tee:
        def __init__(self, *streams): self.streams = streams
        def write(self, s):
            for st in self.streams:
                try: st.write(s)
                except Exception: pass
            return len(s)
        def flush(self):
            for st in self.streams:
                try: st.flush()
                except Exception: pass
        def reconfigure(self, **kw):
            pass

    _log.write(f"\n===== {' '.join(sys.argv[1:])} "
               f"@ {time.strftime('%H:%M:%S')} =====\n")
    _log.flush()
    sys.stdout = _Tee(sys.stdout, _log)
    try:
        raise SystemExit(main())
    finally:
        _log.flush()

