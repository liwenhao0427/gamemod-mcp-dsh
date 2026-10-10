#!/usr/bin/env python3
"""定向复现：`wait_ready` 的时间预算可能被一次**不返回**的 Frida 同步 RPC 架空。

观察到的现场（一次，未复现）：
  · 通过 DSH 调 `launch_game(exe=ved.exe)`，工具调用 **11 分钟以上不返回**；
  · 期间连 `get_session`（纯读内存、不碰 Frida）也一直超时；
  · 被启动的 ved 停在 `BepInEx ... - ved`，窗口标题是 **`Fatal error in GC`**。

代码层面的解释（读源码得出）：
  `RuntimeSession._call` 用 `self._script.exports_sync.init(...)`，
  **Frida 的同步 RPC 没有超时参数**；而 `wait_ready` 的 `deadline` 只在
  **每次调用返回之后**才被检查。所以只要有一次 `init` 不返回：
    · `wait_ready` 的 60 秒预算整体失效 → `launch_game` 永不返回；
    · 该工具是同步函数（`def`），在事件循环里跑 → 整个服务器停摆。

本脚本的做法：spawn → resume → **立刻**用看门狗线程反复调 `init`，
每次调用若 15 秒不返回就记为命中，并把游戏当时的窗口标题打出来。
重复若干轮，用次数说话（单次观测不算结论）。

用法：
    python .cache-test\wedge_repro.py [轮数]
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import frida

EXE = r"E:\project\Ved\ved.exe"
CWD = r"E:\project\Ved"
AGENT = r"E:\project\GameModMCP\src\gamemod_mcp\runtime\agent.js"
_u32 = ctypes.windll.user32


def window_title(pid: int) -> str:
    found: list[str] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(hwnd, _lp):
        w = ctypes.c_ulong()
        _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(w))
        if w.value == pid:
            n = _u32.GetWindowTextLengthW(hwnd)
            if n:
                b = ctypes.create_unicode_buffer(n + 1)
                _u32.GetWindowTextW(hwnd, b, n + 1)
                if b.value.strip():
                    found.append(b.value)
        return True

    _u32.EnumWindows(cb, None)
    return found[0] if found else ""


def kill_ved() -> None:
    subprocess.run(["taskkill", "/IM", "ved.exe", "/F"], capture_output=True)


def one_round(i: int) -> dict:
    out = {"round": i, "hang": False, "title_at_hang": "",
           "init_count": 0, "ready_at": None, "max_call_s": 0.0}
    kill_ved()
    time.sleep(4)
    dev = frida.get_local_device()
    agent = open(AGENT, encoding="utf-8").read()
    pid = dev.spawn([EXE], cwd=CWD, stdio="pipe")
    sess = dev.attach(pid)
    script = sess.create_script(agent, name="gamemod-probe")
    script.load()
    dev.resume(pid)
    t0 = time.time()
    print(f"\n===== 第 {i} 轮 pid={pid} @ {time.strftime('%H:%M:%S')} =====")
    deadline = time.time() + 75
    while time.time() < deadline:
        out["init_count"] += 1
        box: list = []

        def call():
            try:
                box.append(script.exports_sync.init(None))
            except Exception as e:  # noqa: BLE001
                box.append({"ok": False, "error": f"{type(e).__name__}: {e}"})

        t_call = time.time()
        th = threading.Thread(target=call, daemon=True)
        th.start()
        th.join(timeout=15)
        dt = time.time() - t_call
        out["max_call_s"] = max(out["max_call_s"], dt)
        if th.is_alive():
            out["hang"] = True
            out["title_at_hang"] = window_title(pid)
            out["hang_after_s"] = round(time.time() - t0, 1)
            print(f"  ** init#{out['init_count']} 15 秒未返回！"
                  f"（t+{time.time()-t0:.1f}s，标题={out['title_at_hang']!r}）")
            # 再给 45 秒，看它到底回不回来（区分「慢」与「永远不回」）
            th.join(timeout=45)
            print(f"  再等 45 秒后：{'仍未返回' if th.is_alive() else '返回了'}"
                  f"（标题={window_title(pid)!r}）")
            out["returned_eventually"] = not th.is_alive()
            break
        r = box[0] if box else {"ok": False, "error": "无返回"}
        if r.get("ok"):
            out["ready_at"] = round(time.time() - t0, 2)
            print(f"  init#{out['init_count']} 就绪 t+{out['ready_at']}s "
                  f"（单次最长 {out['max_call_s']:.2f}s）")
            break
        time.sleep(0.05)      # **故意不加 0.5s 间隔**：把竞态窗口放到最大
    else:
        print(f"  75 秒内未就绪（未命中不返回）")
    # 收尾
    try:
        script.unload()
        sess.detach()
    except Exception as e:
        print(f"  收尾失败: {e}")
    return out


def main() -> int:
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    res = [one_round(i) for i in range(1, rounds + 1)]
    print("\n\n================ 汇总 ================")
    for r in res:
        print(f"  第 {r['round']} 轮: 就绪={r['ready_at']}s init次数={r['init_count']}"
              f" 单次最长={r['max_call_s']:.2f}s"
              f" {'**命中不返回**' if r['hang'] else '正常'}"
              + (f" 命中时标题={r['title_at_hang']!r}"
                 f" t+{r.get('hang_after_s')}s" if r["hang"] else ""))
    hits = sum(1 for r in res if r["hang"])
    print(f"  不返回命中 {hits} / {len(res)}")
    kill_ved()
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    log = open(Path(__file__).with_suffix(".log"), "a", encoding="utf-8")

    class _T:
        def __init__(self, *s): self.s = s
        def write(self, x):
            for st in self.s:
                try: st.write(x); st.flush()
                except Exception: pass
            return len(x)
        def flush(self):
            for st in self.s:
                try: st.flush()
                except Exception: pass
        def reconfigure(self, **k): pass

    log.write(f"\n########## {time.strftime('%H:%M:%S')} argv={sys.argv[1:]}"
              " ##########\n")
    log.flush()
    sys.stdout = _T(sys.stdout, log)
    try:
        raise SystemExit(main())
    finally:
        log.flush()
