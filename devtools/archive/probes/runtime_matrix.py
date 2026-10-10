#!/usr/bin/env python3
"""对一个**已经在运行**的游戏跑完整运行时链路（attach 模式）。

为什么用 attach 而不是 launch_game：
  · 避免 spawn 时机带来的竞态（见 docs 里的运行时报告）；
  · 手工启动能让游戏完整加载到主菜单，运行时工具面对的是稳定状态。
两种都测过，结论一致；attach 更可重复，所以作为横向扫描的默认姿势。

用法：
    python .cache-test\runtime_matrix.py [游戏名过滤]
"""

from __future__ import annotations

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
CACHE = ROOT / ".cache-test" / "runtime-cache"

#: (显示名, 游戏目录, 主程序名, 引擎)
SAMPLES = [
    ("Ved", r"E:\project\Ved", "ved.exe", "il2cpp"),
    ("PathOfIdle", r"E:\project\PathOfIdle", "PathOfIdle.exe", "il2cpp"),
    ("Archon Soul", r"E:\project\Archon Soul", "Archon Soul.exe", "mono"),
    ("Kinny", r"E:\project\Kinny and the Cosmic Cauldron",
     "Kinny and the Cosmic Cauldron.exe", "mono"),
]


def pids_of(stem: str) -> list[int]:
    out = subprocess.run(
        ["pwsh", "-NoProfile", "-Command",
         f"Get-Process -Name '{stem}' -ErrorAction SilentlyContinue |"
         " Sort-Object StartTime | Select-Object -ExpandProperty Id"],
        capture_output=True, text=True)
    return [int(x) for x in out.stdout.split() if x.strip().isdigit()]


def kill(stem: str) -> None:
    subprocess.run(["taskkill", "/IM", f"{stem}.exe", "/F"], capture_output=True)


class Probe:
    def __init__(self) -> None:
        self.srv = McpStdioServer(
            [PY, str(SERVER)],
            env={**script_form_env(str(ROOT / "src"), clean_pythonpath=False),
                 "GAMEMOD_LOG_LEVEL": "INFO"},
            cwd=str(ROOT),
        ).start()
        init = self.srv.initialize(timeout=60)
        tl = self.srv.tools(timeout=60)
        print(f"  initialize ok={init['ok']} 工具 {len(tl['names'])} 个 "
              f"stdout 非 JSON 行={self.srv.non_json_lines}")

    def call(self, name: str, args: dict, timeout: float = 120.0) -> dict:
        t0 = time.time()
        r = self.srv.call_tool(name, args, timeout=timeout)
        dt = time.time() - t0
        d = r.get("data") or {}
        if not r.get("ok"):
            print(f"  {name}: **调用层失败** {dt:.2f}s {r['error'][:300]}")
        elif r.get("is_error"):
            print(f"  {name}: **工具抛异常** {dt:.2f}s {r['error'][:300]}")
        else:
            print(f"  {name}: ok={d.get('ok')} {dt:.2f}s")
        if d.get("error"):
            print(f"     工具报错: {str(d['error'])[:220]}")
        sys.stdout.flush()
        return d

    def close(self) -> None:
        self.srv.close()


def run_one(name: str, game: str, exe: str, engine: str) -> dict:
    stem = Path(exe).stem
    res = {"name": name, "engine": engine, "steps": {}, "fatal": ""}
    print(f"\n===== {name}（{engine}）=====")
    kill(stem)
    time.sleep(3)
    # 必须给**绝对**路径：Windows 的 CreateProcess 不搜索「新 cwd」，
    # 只搜父进程的 PATH —— 传相对名会直接 FileNotFoundError。
    proc = subprocess.Popen([str(Path(game) / exe)], cwd=game)
    print(f"  手工启动 pid={proc.pid}，等 40 秒让它进主界面…")
    time.sleep(40)
    if subprocess.run(["tasklist", "/FI", f"PID eq {proc.pid}"],
                      capture_output=True, text=True).stdout.find(
                          str(proc.pid)) < 0:
        res["fatal"] = "游戏自己退了（手工启动就失败）"
        print(f"  !!! {res['fatal']}")
        return res

    p = Probe()
    try:
        r = p.call("open_game", {"game_dir": game, "cache_dir": str(CACHE)})
        res["steps"]["open_game"] = r.get("ok")
        r = p.call("inject_process", {"pid": proc.pid}, timeout=180)
        res["steps"]["inject_process"] = r.get("ok")
        res["inject_error"] = r.get("error", "")
        res["init"] = r.get("init")
        res["image_count_matches"] = r.get("image_count_matches")

        r = p.call("runtime_info", {})
        res["steps"]["runtime_info"] = r.get("ok")
        res["image_count"] = r.get("image_count")
        res["static_image_count"] = r.get("static_image_count")
        res["image_count_matches"] = r.get("image_count_matches", res.get(
            "image_count_matches"))

        # 1) 字段偏移：拿一个必然存在的框架类
        r = p.call("runtime_class_info",
                   {"name": "UnityEngine.Transform"}, timeout=60)
        res["steps"]["runtime_class_info(Transform)"] = r.get("ok")
        flds = r.get("fields") or []
        res["offset_sample"] = next(
            ({k: f.get(k) for k in ("name", "type", "offset")}
             for f in flds if f.get("offset")), None)
        ms = r.get("methods") or []
        res["method_addr_sample"] = next(
            ({k: m.get(k) for k in ("name", "token", "pointer", "rva_hex",
                                    "in_module")}
             for m in ms if m.get("pointer")), None)

        # 2) 静态↔运行时 token 对账（配方 4 的核心）
        st = p.call("class_detail",
                    {"full_name": "UnityEngine.Time",
                     "method_name": "get_frameCount"}, timeout=60)
        static_toks = [m.get("token") for m in (st.get("methods") or [])]
        res["static_tokens"] = static_toks
        rt = p.call("find_hook_target",
                    {"name": "Time", "method": "get_frameCount",
                     "namespace": "UnityEngine"}, timeout=60)
        cands = rt.get("candidates") or []
        res["runtime_tokens"] = [c.get("token") for c in cands]
        res["token_match"] = bool(
            set(t.lower() for t in static_toks if t)
            & set(t.lower() for t in res["runtime_tokens"] if t))

        # 3) Hook + 事件
        h = p.call("hook_method",
                   {"name": "Time", "method": "get_frameCount",
                    "namespace": "UnityEngine", "args_type": "",
                    "label": "frame", "max_calls": 100}, timeout=60)
        res["steps"]["hook_method"] = h.get("ok")
        res["chosen_token"] = (h.get("chosen") or {}).get("token")
        time.sleep(4)
        hs = p.call("hook_status", {}, timeout=60)
        res["hook_calls"] = (hs.get("hooks") or [{}])[0].get("calls") \
            if hs.get("hooks") else hs.get("calls")
        ev = p.call("drain_events", {"limit": 100, "kind": "hook"}, timeout=60)
        evs = ev.get("events") or []
        res["hook_events"] = len(evs)
        res["sample_event"] = evs[0] if evs else None

        # 4) GC 开关（Mono 上预期 ok=false，这不是故障）
        g = p.call("gc_control", {"disable": True}, timeout=60)
        res["gc_control_ok"] = g.get("ok")
        res["gc_control_error"] = g.get("error", "")
        if g.get("ok"):
            p.call("gc_control", {"disable": False}, timeout=60)

        # 5) 收尾
        r = p.call("unhook", {}, timeout=60)
        res["steps"]["unhook"] = r.get("ok")
        c = p.call("close_session", {}, timeout=180)
        res["steps"]["close_session"] = c.get("ok")
        res["close_error"] = c.get("error", "")
        res["domain_released"] = ((c.get("runtime") or {}).get("steps") or {}) \
            .get("domain_released")
        res["runtime_steps"] = (c.get("runtime") or {}).get("steps")
    finally:
        p.close()
        # 观察 detach 后是否被 GC 崩掉（历史缺陷的现场）
        print("  detach 后观察 20 秒…")
        alive_after = []
        for _ in range(4):
            time.sleep(5)
            alive_after.append(len(pids_of(stem)) > 0)
        res["alive_after_detach"] = alive_after
        print(f"  存活采样: {alive_after}")
    kill(stem)
    return res


def main() -> int:
    only = sys.argv[1] if len(sys.argv) > 1 else ""
    out = []
    for name, game, exe, engine in SAMPLES:
        if only and only.lower() not in name.lower():
            continue
        if not Path(game).is_dir():
            print(f"\n===== {name} 跳过（目录不存在）=====")
            continue
        out.append(run_one(name, game, exe, engine))
    print("\n\n================ 汇总 ================")
    for r in out:
        print(f"\n■ {r['name']}（{r['engine']}）")
        print(f"  步骤: {r['steps']}")
        print(f"  镜像数 运行时={r.get('image_count')} "
              f"静态={r.get('static_image_count')} "
              f"一致={r.get('image_count_matches')}")
        print(f"  字段偏移样本: {r.get('offset_sample')}")
        print(f"  方法地址样本: {r.get('method_addr_sample')}")
        print(f"  token 对账: 静态={r.get('static_tokens')} "
              f"运行时={r.get('runtime_tokens')} 命中={r.get('token_match')}")
        print(f"  hook: ok={r['steps'].get('hook_method')} "
              f"chosen={r.get('chosen_token')} 触发={r.get('hook_calls')} "
              f"事件={r.get('hook_events')}")
        print(f"  事件样本: {r.get('sample_event')}")
        print(f"  gc_control: ok={r.get('gc_control_ok')} "
              f"err={str(r.get('gc_control_error'))[:80]}")
        print(f"  close: ok={r['steps'].get('close_session')} "
              f"domain_released={r.get('domain_released')} "
              f"err={str(r.get('close_error'))[:120]}")
        print(f"  detach 后存活: {r.get('alive_after_detach')}")
        if r.get("fatal"):
            print(f"  !! {r['fatal']}")
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

    log.write(f"\n########## runtime_matrix @ {time.strftime('%H:%M:%S')}"
              f" argv={sys.argv[1:]} ##########\n")
    log.flush()
    sys.stdout = _Tee(sys.stdout, log)
    try:
        raise SystemExit(main())
    finally:
        log.flush()
