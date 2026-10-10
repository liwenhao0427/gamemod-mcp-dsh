#!/usr/bin/env python3
"""验证缓解措施：`wait_ready` 的 settle 延迟是否真的降低了卡死率。

背景（`devtools/archive/probes/wedge_ab.py` 的对照实验，Ved 上各 8 轮）：

| 第一次探测前的静默 | 探测间隔 | 卡死率 |
|---|---|---|
| 0 秒 | 0（紧循环） | 8/8 |
| 0 秒 | 0.1 秒 | 1/8 |
| 0 秒 | 0.5 秒 | 0/8 |
| 3 秒 | 0（紧循环） | 0/8 |

本脚本验证的是**产品代码路径**：直接调 `RuntimeSession.wait_ready`
（而不是手写轮询），在**最恶劣条件**（探测间隔 0）下跑 N 轮。
若缓解生效，卡死率应显著低于不加 settle 的 8/8。

**为什么必须走产品代码**：手写轮询测的是「脚本多激进」，
而这里要证明的是「产品里那个常量真的起作用」。

用法：
    python .cache-test\\verify_settle.py [轮数]
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import frida

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from gamemod_mcp.runtime import session as sess_mod  # noqa: E402

EXE = r"E:\project\Ved\ved.exe"
CWD = r"E:\project\Ved"
AGENT = Path(r"E:\project\GameModMCP\src\gamemod_mcp\runtime\agent.js")
OUT = Path(__file__).with_suffix(".jsonl")
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


def kill_game() -> None:
    subprocess.run(["taskkill", "/IM", "ved.exe", "/F"], capture_output=True)


def one_round(i: int, settle: float, interval: float) -> dict:
    """走**产品** `wait_ready`，探测间隔拉到最恶劣。"""
    rec: dict = {"round": i, "settle": settle, "interval": interval,
                 "wedged": False, "ready": False, "title": ""}
    kill_game()
    time.sleep(4)
    dev = frida.get_local_device()
    pid = dev.spawn([EXE], cwd=CWD, stdio="pipe")
    sess = dev.attach(pid)
    script = sess.create_script(AGENT.read_text(encoding="utf-8"),
                                name="gamemod-probe")
    script.load()
    rs = sess_mod.RuntimeSession(pid, sess, script, name="ved.exe",
                                 spawned=True)
    rs._call_timeout = 12.0
    dev.resume(pid)

    t0 = time.time()
    try:
        # interval=0 让 wait_ready 紧循环；settle 由参数决定
        r = rs.wait_ready(timeout=40.0, interval=interval, settle=settle)
        rec["ready"] = bool(r.get("ok"))
        rec["wedged"] = bool(r.get("wedged"))
        rec["attempts"] = r.get("attempts")
        rec["waited"] = r.get("waited")
    except Exception as exc:  # noqa: BLE001
        rec["error"] = f"{type(exc).__name__}: {exc}"
        rec["wedged"] = True
    rec["seconds"] = round(time.time() - t0, 1)
    rec["title"] = window_title(pid)
    if not rec["wedged"] and rec["ready"]:
        t1 = time.time()
        while time.time() - t1 < 25:
            time.sleep(3)
            rec["title"] = window_title(pid)
            if rec["title"] == "VED":
                break
    if not rec["wedged"] and "Fatal error" in (rec["title"] or ""):
        rec["wedged"] = True
        rec["wedged_by"] = "title"
    # 收尾必须**有界**：卡死时 unload/detach 同样会挂住（报告 §4.2 的教训）
    if not rec["wedged"]:
        try:
            script.unload()
            sess.detach()
        except Exception as exc:  # noqa: BLE001
            rec["cleanup_error"] = str(exc)
    else:
        rec["cleanup_skipped"] = "wedged"
    return rec


def main() -> int:
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    settle = float(os.environ.get("SETTLE", str(sess_mod.READY_SETTLE_DELAY)))
    interval = float(os.environ.get("INTERVAL", "0"))
    print(f"轮数={rounds} settle={settle:g}s interval={interval:g}s "
          f"（interval=0 = 最恶劣的紧循环）")
    recs = []
    for i in range(1, rounds + 1):
        r = one_round(i, settle, interval)
        recs.append(r)
        verdict = "卡死" if r["wedged"] else ("正常" if r["ready"] else "未就绪")
        print(f"  第 {i}/{rounds} 轮: {verdict} attempts={r.get('attempts')} "
              f"耗时={r['seconds']}s 标题={r['title']!r}", flush=True)
        with open(OUT, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    kill_game()
    wedged = sum(1 for r in recs if r["wedged"])
    ready = sum(1 for r in recs if r["ready"] and not r["wedged"])
    print(f"\nsettle={settle:g}s interval={interval:g}s: "
          f"卡死 {wedged}/{len(recs)}，正常就绪 {ready}/{len(recs)}")
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

    log.write(f"\n##### {time.strftime('%H:%M:%S')} argv={sys.argv[1:]} #####\n")
    log.flush()
    sys.stdout = _T(sys.stdout, log)
    try:
        raise SystemExit(main())
    finally:
        log.flush()
