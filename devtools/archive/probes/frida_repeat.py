#!/usr/bin/env python3
"""重复跑 frida 实验形态，统计「init 不返回 / 游戏卡住」的出现率。

为什么必须重复：E 与 G 走的是**同一条代码路径**（resume → 加载探针 → 轮询 init），
一次卡死、一次正常 —— 单次观测不能当结论。AGENTS.md 那条「样本存活结论必须先
确认观察窗口足够长」的教训在这里同样适用：竞态必须靠重复次数说话。

用法：
    python .cache-test\frida_repeat.py <mode> [次数] [每轮观察秒数]
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = r"C:\Users\temp\AppData\Local\Programs\Python\Python310\python.exe"
EXE = r"E:\project\Ved\ved.exe"


def kill_ved() -> None:
    subprocess.run(["taskkill", "/IM", "ved.exe", "/F"], capture_output=True)


def main() -> int:
    mode = sys.argv[1]
    rounds = int(sys.argv[2]) if len(sys.argv) > 2 else 4
    observe = sys.argv[3] if len(sys.argv) > 3 else "45"
    summary = []
    for i in range(1, rounds + 1):
        kill_ved()
        time.sleep(4)
        print(f"\n===== 形态 {mode} 第 {i}/{rounds} 轮 @ "
              f"{time.strftime('%H:%M:%S')} =====", flush=True)
        env = {**os.environ, "OBSERVE": observe,
               "LOG": str(HERE / f"matrix_{mode}.log")}
        p = subprocess.Popen([PY, str(HERE / "frida_matrix.py"), mode, EXE],
                             env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True,
                             encoding="utf-8", errors="replace")
        # 每轮给足上限：卡住的形态必须能被**超时**截断，否则整批跑不完。
        try:
            out, _ = p.communicate(timeout=200)
        except subprocess.TimeoutExpired:
            p.kill()
            out, _ = p.communicate()
            out = (out or "") + "\n[驱动] 本轮 200 秒未结束，已强制终止"
        tail = [ln for ln in (out or "").splitlines() if ln.strip()]
        for ln in tail[-12:]:
            print("   " + ln, flush=True)
        verdict = "?"
        if "到达目标标题 'VED'" in (out or ""):
            verdict = "正常进主场景"
        elif "进程已消失" in (out or ""):
            verdict = "进程消失"
        elif "未返回" in (out or ""):
            verdict = "init 不返回"
        elif "本轮 200 秒未结束" in (out or ""):
            verdict = "整轮卡死"
        elif "观察结束" in (out or ""):
            verdict = "卡在 preloader"
        print(f"   判定: {verdict}", flush=True)
        summary.append(verdict)
    print(f"\n===== 形态 {mode} 汇总 =====")
    for i, v in enumerate(summary, 1):
        print(f"  第 {i} 轮: {v}")
    ok = sum(1 for v in summary if v == "正常进主场景")
    print(f"  正常 {ok} / {len(summary)}")
    kill_ved()
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    log = open(HERE / f"repeat_{sys.argv[1]}.log", "a", encoding="utf-8")

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
