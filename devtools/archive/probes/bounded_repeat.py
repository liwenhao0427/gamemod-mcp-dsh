#!/usr/bin/env python3
"""有界的、**抗挂死**的复现统计：真实 MCP `launch_game` 的卡死率。

为什么另写一个：上一版 `frida_repeat.py` 自己会挂 —— 因为一旦探针线程卡在
**不返回的 Frida 同步 RPC** 里，`script.unload()` / `session.detach()` 也是
同步调用、同样没有超时，于是连「收尾」都回不来，驱动脚本跟着一起挂 18 分钟。
这本身就是缺陷的一部分（不只是游戏侧），所以驱动必须**按进程树硬杀**。

本脚本的关键设计：
  · 每个子轮次是**独立进程**，由 `subprocess` 起；
  · 用 `taskkill /T /F` 杀**整棵树**（子 python + 它 spawn 的游戏）；
  · 结果**逐轮追加落盘**，所以中途被打断也不丢已得数据；
  · 每轮有硬上限，超时记为一个明确结果，而不是让脚本悬住。

用法：
    python .cache-test\bounded_repeat.py [轮数] [每轮上限秒]
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PY = r"C:\Users\temp\AppData\Local\Programs\Python\Python310\python.exe"
EXE = r"E:\project\Ved\ved.exe"
GAME = r"E:\project\Ved"
OUT = HERE / "bounded_repeat.jsonl"
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


def kill_tree(pid: int) -> None:
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                   capture_output=True)


def kill_game() -> None:
    subprocess.run(["taskkill", "/IM", "ved.exe", "/F"], capture_output=True)


#: 子轮次脚本：只做「真实 launch_game + 记录标题」，每步都打点。
CHILD = r'''
import json, sys, time, threading
sys.path.insert(0, r"{devtools}")
from mcp_stdio import McpStdioServer, script_form_env
from pathlib import Path
import ctypes
_u32 = ctypes.windll.user32
def title(pid):
    f = []
    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(h, _l):
        w = ctypes.c_ulong(); _u32.GetWindowThreadProcessId(h, ctypes.byref(w))
        if w.value == pid:
            n = _u32.GetWindowTextLengthW(h)
            if n:
                b = ctypes.create_unicode_buffer(n+1); _u32.GetWindowTextW(h, b, n+1)
                if b.value.strip(): f.append(b.value)
        return True
    _u32.EnumWindows(cb, None)
    return f[0] if f else ""
ROOT = Path(r"{root}")
srv = McpStdioServer([r"{py}", str(ROOT/"src/gamemod_mcp/server.py")],
    env={{**script_form_env(str(ROOT/"src"), clean_pythonpath=False),
         "GAMEMOD_LOG_LEVEL": "WARNING"}}, cwd=str(ROOT)).start()
srv.initialize(timeout=60); srv.tools(timeout=60)
srv.call_tool("open_game", {{"game_dir": r"{game}", "cache_dir": r"{cache}"}}, timeout=180)
t0 = time.time()
r = srv.call_tool("launch_game", {{"exe": r"{exe}"}}, timeout={launch_to})
print(json.dumps({{"step": "launch", "seconds": round(time.time()-t0,2),
                   "call_ok": r["ok"], "err": r["error"][:200],
                   "data": (r.get("data") or {{}}).get("pid")}}), flush=True)
if not r["ok"]:
    print(json.dumps({{"step": "done", "verdict": "launch 调用超时/失败"}}), flush=True)
    raise SystemExit(0)
pid = (r.get("data") or {{}}).get("pid")
final = ""
t1 = time.time()
while time.time()-t1 < {observe}:
    time.sleep(3)
    final = title(pid)
    if final == "VED": break
print(json.dumps({{"step": "title", "title": final,
                   "verdict": "正常进主场景" if final == "VED" else f"卡住:{{final}}"}}), flush=True)
'''


def main() -> int:
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    per_round_cap = float(sys.argv[2]) if len(sys.argv) > 2 else 100.0
    cache = str(HERE / "bounded-cache")
    if OUT.exists():
        OUT.unlink()
    print(f"轮数 {rounds}，每轮硬上限 {per_round_cap:.0f}s；结果落盘 {OUT}\n")
    for i in range(1, rounds + 1):
        kill_game()
        time.sleep(4)
        child = HERE / f"_child_{i}.py"
        child.write_text(CHILD.format(
            devtools=str(ROOT / "devtools"), root=str(ROOT), py=PY,
            game=GAME, exe=EXE, cache=cache,
            launch_to=int(per_round_cap * 0.6), observe=int(per_round_cap * 0.3)),
            encoding="utf-8")
        t0 = time.time()
        p = subprocess.Popen([PY, str(child)], stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True,
                             encoding="utf-8", errors="replace")
        # 硬上限：到点就杀**整棵树**（子 python + 它 spawn 的游戏）
        verdict = "?"
        lines = []
        try:
            out, _ = p.communicate(timeout=per_round_cap)
            lines = [ln for ln in (out or "").splitlines() if ln.strip()]
        except subprocess.TimeoutExpired:
            kill_tree(p.pid)
            try:
                out, _ = p.communicate(timeout=15)
                lines = [ln for ln in (out or "").splitlines() if ln.strip()]
            except Exception:
                pass
            verdict = f"整轮卡死（>{per_round_cap:.0f}s 未结束）"
        # 解析子进程最后一条 JSON 打点作为判定
        if verdict == "?":
            for ln in reversed(lines):
                if ln.startswith("{"):
                    try:
                        d = json.loads(ln)
                    except json.JSONDecodeError:
                        continue
                    if d.get("verdict"):
                        verdict = d["verdict"]
                        break
            else:
                verdict = "无判定（子进程异常退出）"
        rec = {"round": i, "seconds": round(time.time() - t0, 1),
               "verdict": verdict, "lines": lines[-6:]}
        with open(OUT, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"第 {i}/{rounds} 轮: {verdict}  （{rec['seconds']}s）")
        for ln in lines[-4:]:
            print(f"    {ln}")
        sys.stdout.flush()
        child.unlink(missing_ok=True)
    kill_game()

    recs = [json.loads(x) for x in OUT.read_text(encoding="utf-8").splitlines()
            if x.strip()]
    ok = sum(1 for r in recs if r["verdict"] == "正常进主场景")
    print(f"\n===== 汇总（{len(recs)} 轮）=====")
    for r in recs:
        print(f"  第 {r['round']} 轮: {r['verdict']}")
    print(f"  正常 {ok} / {len(recs)}；异常 {len(recs)-ok} / {len(recs)}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
