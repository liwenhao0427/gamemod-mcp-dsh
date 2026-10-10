#!/usr/bin/env python3
"""一次性侦察脚本：用**独立**的 stdio MCP 服务器进程复现运行时链路。

为什么不直接用 DSH 里那个长驻服务器：它一旦卡住，整个 Agent 会话就
拿不到任何工具返回（实测发生过）。这里每次新起一个进程，卡住只影响本脚本，
且能把每一步的耗时与 stderr 原文带出来。

用法：
    python .cache-test\probe_check.py <scenario> [args...]

scenario:
    static <game_dir>                     只做静态链路
    attach <game_dir> <exe_name>          注入已在运行的进程
    spawn  <game_dir> <exe>               启动并注入
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

# 仓库根**自寻**：向上找含 src/gamemod_mcp/server.py 的那一层。
# 不写死 .parent 的层数 —— 这些脚本曾被从 devtools/ 挪进 devtools/archive/probes/，
# 当时写死的 parent.parent 就指到了 devtools/archive，SERVER 变成不存在的路径，
# 每个脚本都在 tools/list 崩在 OSError: [Errno 22]，看起来却像「MCP 服务器起不来」。
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


class Runner:
    def __init__(self, label: str) -> None:
        self.label = label
        self.srv = McpStdioServer(
            [PY, str(SERVER)],
            env={**script_form_env(str(ROOT / "src"), clean_pythonpath=False),
                 "PYTHONPATH": str(ROOT / "src"), "GAMEMOD_LOG_LEVEL": "INFO"},
            cwd=str(ROOT),
        ).start()
        init = self.srv.initialize(timeout=60)
        tl = self.srv.tools(timeout=60)
        print(f"[{label}] initialize ok={init['ok']} tools={len(tl['names'])} "
              f"non_json_stdout={self.srv.non_json_lines}")

    def call(self, name: str, args: dict, timeout: float = 120.0) -> dict:
        t0 = time.time()
        r = self.srv.call_tool(name, args, timeout=timeout)
        dt = time.time() - t0
        data = r.get("data") or {}
        brief = {k: data.get(k) for k in
                 ("ok", "error", "warning", "total", "type_count", "pid",
                  "engine", "context_switched", "context_warning", "reused")
                 if k in data}
        print(f"[{self.label}] {name}({_short(args)}) -> {dt:.2f}s {brief}")
        if not r.get("ok"):
            print(f"[{self.label}]   调用层失败: {r.get('error')[:600]}")
        if r.get("is_error"):
            print(f"[{self.label}]   工具抛异常: {r.get('error')[:600]}")
        if data and data.get("error"):
            print(f"[{self.label}]   工具报错: {str(data['error'])[:600]}")
        sys.stdout.flush()
        return data

    def close(self) -> None:
        print(f"[{self.label}] stderr 末尾:\n{self.srv.stderr_tail(25)}")
        self.srv.close()


def _short(args: dict) -> str:
    out = []
    for k, v in args.items():
        s = str(v)
        out.append(f"{k}={s if len(s) < 60 else s[:57] + '...'}")
    return ", ".join(out)


def find_pid(exe_name: str) -> int:
    out = subprocess.run(
        ["pwsh", "-NoProfile", "-Command",
         f"(Get-Process -Name '{Path(exe_name).stem}' -ErrorAction SilentlyContinue |"
         " Sort-Object StartTime | Select-Object -Last 1).Id"],
        capture_output=True, text=True)
    txt = out.stdout.strip()
    return int(txt) if txt.isdigit() else 0


def scenario_static(r: Runner, game_dir: str) -> None:
    r.call("open_game", {"game_dir": game_dir, "cache_dir": str(CACHE)})
    r.call("list_images", {"limit": 5})
    r.call("search_classes", {"pattern": "Manager", "limit": 3})
    r.call("class_detail", {"full_name": "LocalizationManager",
                            "assembly": "Assembly-CSharp"})
    r.call("get_session", {})


def scenario_attach(r: Runner, game_dir: str, exe_name: str) -> None:
    r.call("open_game", {"game_dir": game_dir, "cache_dir": str(CACHE)})
    pid = find_pid(exe_name)
    print(f"[{r.label}] 目标 pid={pid}")
    if not pid:
        print(f"[{r.label}] 目标进程没在跑，跳过")
        return
    r.call("inject_process", {"pid": pid}, timeout=180)
    _runtime_chain(r)
    r.call("close_session", {}, timeout=180)


def scenario_spawn(r: Runner, game_dir: str, exe: str) -> None:
    r.call("open_game", {"game_dir": game_dir, "cache_dir": str(CACHE)})
    r.call("launch_game", {"exe": exe}, timeout=180)
    _runtime_chain(r)
    r.call("close_session", {}, timeout=180)


def _runtime_chain(r: Runner) -> None:
    r.call("runtime_info", {}, timeout=60)
    r.call("runtime_class_info", {"name": "UnityEngine.Time"}, timeout=60)
    r.call("runtime_class_info", {"name": "LocalizationManager"}, timeout=60)
    r.call("find_hook_target", {"name": "Time", "method": "get_frameCount",
                               "namespace": "UnityEngine"}, timeout=60)
    r.call("hook_method", {"name": "Time", "method": "get_frameCount",
                           "namespace": "UnityEngine", "args_type": "",
                           "label": "frame", "max_calls": 50}, timeout=60)
    time.sleep(3)
    r.call("hook_status", {}, timeout=60)
    ev = r.call("drain_events", {"limit": 50, "kind": "hook"}, timeout=60)
    print(f"[{r.label}]   hook 事件 {len(ev.get('events') or [])} 条")
    r.call("unhook", {}, timeout=60)
    r.call("gc_control", {"disable": True}, timeout=60)
    r.call("gc_control", {"disable": False}, timeout=60)


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    sc = sys.argv[1]
    r = Runner(sc)
    try:
        if sc == "static":
            scenario_static(r, sys.argv[2])
        elif sc == "attach":
            scenario_attach(r, sys.argv[2], sys.argv[3])
        elif sc == "spawn":
            scenario_spawn(r, sys.argv[2], sys.argv[3])
        else:
            print(f"未知场景 {sc}")
            return 2
    finally:
        r.close()
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
