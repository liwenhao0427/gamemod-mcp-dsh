#!/usr/bin/env python3
"""验证：`open_game` 失败时**仍然改写了会话状态**（失败带副作用）。

现场（实测，不是推测）：
    open_game(game_dir="E:\\project")   → ok:false（正确拒绝：容器目录）
    紧接着 get_session()                → game_dir="E:\\project"、engine="unknown"、
                                          exe="E:\\project\\Im.No.Demonlord...\\ImNoDemonlord.exe"

后一条是**新的**：调用方得到的是「失败」，会话里却已经换成了那个目录，
而且 `exe` 被填成了**某个子目录里**的主程序。后果不是「数字看着怪」——
`launch_game()` 不传参时默认用会话里的 `exe`，也就是会去启动容器目录下的
某一款游戏（正是 docs/多游戏兼容性扫描报告.md §3.1 警告的最坏情况）。

对照：`ensure_runtime` 里为「失败还带副作用」专门写过一段注释并做了
「先连新的、连上了才断旧的」修复 —— 同类问题在 `select_game` 这条路上还在。

用法：
    python .cache-test\failed_call_side_effect.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
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
CACHE = ROOT / ".cache-test" / "sideeffect-cache"


def main() -> int:
    srv = McpStdioServer(
        [PY, str(SERVER)],
        env={**script_form_env(str(ROOT / "src"), clean_pythonpath=False),
             "GAMEMOD_LOG_LEVEL": "WARNING"},
        cwd=str(ROOT)).start()
    try:
        srv.initialize()
        srv.tools()

        print("① 先选一个**真实存在**的游戏，建立基线")
        r1 = srv.call_tool("open_game", {"game_dir": r"E:\project\Ved",
                                         "cache_dir": str(CACHE)}, timeout=180)
        d1 = r1["data"]
        print(f"   ok={d1['ok']} game_dir={d1['game_dir']!r} "
              f"engine={d1['engine']!r} exe={d1['exe']!r}")

        print("\n② 调用一个**必定失败**的 open_game（容器目录）")
        r2 = srv.call_tool("open_game", {"game_dir": r"E:\project"},
                           timeout=180)
        d2 = r2["data"]
        print(f"   ok={d2['ok']}（这一条是正确的：明确拒绝）")
        print(f"   error={str(d2.get('error'))[:150]}")

        print("\n③ 关键：失败之后，会话被改成什么了？")
        r3 = srv.call_tool("get_session", {}, timeout=60)
        d3 = r3["data"]
        print(f"   game_dir={d3['game_dir']!r}")
        print(f"   engine  ={d3['engine']!r}")
        print(f"   exe     ={d3['exe']!r}")
        print(f"   index_ready={d3['index_ready']} runtime_ready={d3['runtime_ready']}")

        exh = (d3["exe"] or "").lower()
        subdir_exe = exh.startswith(r"e:\project\im.no.")
        polluted = (d3["game_dir"] == r"E:\project") or subdir_exe
        lost = (d2["ok"] is False) and (d1["game_dir"] != d3["game_dir"])
        print("\n判定:")
        print(f"  · 失败的调用污染了会话身份（game_dir/exe 变了）：{polluted}")
        print(f"  · 原本可用的会话（Ved）被推倒：{lost}")
        print(f"  · 被填进去的 exe 在**子目录**里、不是 E:\\project 的主程序："
              f"{subdir_exe}")

        if polluted:
            print("\n  危害：launch_game() 不传 exe 时会用会话里的这个 exe ——")
            print("        也就是会去启动容器目录下的某一款游戏，"
                  "而调用方以为自己只是问了一句「这个目录行不行」。")
            print("        对照 ensure_runtime 的 switching_from 注释："
                  "那处专门修过「失败带副作用」，select_game 这条还没。")
    finally:
        srv.close()
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

    sys.stdout = _T(sys.stdout, log)
    try:
        raise SystemExit(main())
    finally:
        log.flush()
