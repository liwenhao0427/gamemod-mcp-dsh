#!/usr/bin/env python3
"""重复跑真实的 MCP `launch_game`，统计「游戏卡在 preloader」的发生率。

为什么要重复：E 与 G 两次实验的代码路径几乎相同，结果却一次卡死、一次正常 ——
说明这是个**竞态**，单次观测不能当结论（AGENTS.md 里那条「样本存活结论必须先
确认观察窗口足够长」的教训同理适用）。所以这里跑 N 次，逐次记录标题。

判据用窗口标题：`BepInEx ... - ved` = 卡在 preloader；`VED` = 正常进主窗口。
每次跑完都杀掉游戏，保证下一次是干净起点。

用法：
    python .cache-test\repeat_launch.py [次数] [观察秒数]
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from pathlib import Path

# 仓库根**自寻**（理由见 runtime_matrix.py 的说明）：不写死 .parent 层数。
def _repo_root() -> Path:
    for p in Path(__file__).resolve().parents:
        if (p / "src" / "gamemod_mcp" / "server.py").is_file():
            return p
    raise SystemExit("找不到仓库根（没有 src/gamemod_mcp/server.py）")


ROOT = _repo_root()
sys.path.insert(0, str(ROOT / "devtools"))
from mcp_stdio import McpStdioServer, script_form_env  # noqa: E402

PY = r"C:\Users\temp\AppData\Local\Programs\Python\Python310\python.exe"
SERVER = ROOT / "src" / "gamemod_mcp" / "server.py"
CACHE = ROOT / ".cache-test" / "probe-cache"
EXE = r"E:\project\Ved\ved.exe"
GAME = r"E:\project\Ved"

_u32 = ctypes.windll.user32


def window_title(pid: int) -> str:
    found: list[str] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(hwnd, _lp):
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


def kill_ved() -> None:
    subprocess.run(["taskkill", "/IM", "ved.exe", "/F"],
                   capture_output=True)


def main() -> int:
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    wait = float(sys.argv[2]) if len(sys.argv) > 2 else 45.0
    results = []
    for i in range(1, rounds + 1):
        kill_ved()
        time.sleep(4)
        print(f"\n===== 第 {i}/{rounds} 轮 @ {time.strftime('%H:%M:%S')} =====")
        srv = McpStdioServer(
            [PY, str(SERVER)],
            env={**script_form_env(str(ROOT / "src"), clean_pythonpath=False),
                 "GAMEMOD_LOG_LEVEL": "INFO"},
            cwd=str(ROOT),
        ).start()
        try:
            srv.initialize(timeout=60)
            srv.tools(timeout=60)
            r = srv.call_tool("open_game",
                              {"game_dir": GAME, "cache_dir": str(CACHE)},
                              timeout=120)
            # launch_game 常常不返回（实测卡到客户端超时），所以给一个
            # **有限**超时，超时也算一条真实观测，不当成脚本错误。
            t0 = time.time()
            lr = srv.call_tool("launch_game", {"exe": EXE}, timeout=90)
            dt = time.time() - t0
            data = lr.get("data") or {}
            print(f"launch_game: 调用层 ok={lr['ok']} 用时 {dt:.1f}s "
                  f"pid={data.get('pid')} warning={data.get('warning')}")
            if not lr["ok"]:
                print(f"  调用层错误: {lr['error'][:200]}")
            pid = data.get("pid") or 0
            if pid:
                t1 = time.time()
                title = ""
                while time.time() - t1 < wait:
                    time.sleep(3)
                    title = window_title(pid)
                    if "VED" == title:
                        break
                    if not title and time.time() - t1 > 15:
                        print(f"  t+{time.time()-t1:.0f}s 无窗口（进程可能已退）")
                        break
                # 二次确认：再用一次工具，看服务器是否还能回话
                t2 = time.time()
                qi = srv.call_tool("runtime_info", {}, timeout=30)
                print(f"  最终标题={title!r}  runtime_info: ok={qi['ok']} "
                      f"用时 {time.time()-t2:.1f}s")
                verdict = "正常进主场景" if title == "VED" else f"卡住（{title!r}）"
            else:
                verdict = "没有拿到 pid"
            print(f"  判定: {verdict}")
            results.append(verdict)
        finally:
            srv.close()
    print("\n===== 汇总 =====")
    for i, v in enumerate(results, 1):
        print(f"  第 {i} 轮: {v}")
    ok = sum(1 for v in results if v == "正常进主场景")
    print(f"  正常 {ok} / {len(results)}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    log = open(os.environ.get("LOG", str(Path(__file__).with_suffix(".log"))),
               "a", encoding="utf-8")

    class _Tee:
        def __init__(self, *s): self.s = s
        def write(self, x):
            for st in self.s:
                try: st.write(x)
                except Exception: pass
            return len(x)
        def flush(self):
            for st in self.s:
                try: st.flush()
                except Exception: pass
        def reconfigure(self, **kw): pass

    log.write(f"\n########## repeat @ {time.strftime('%H:%M:%S')} "
              f"argv={sys.argv[1:]} ##########\n")
    log.flush()
    sys.stdout = _Tee(sys.stdout, log)
    try:
        raise SystemExit(main())
    finally:
        log.flush()
