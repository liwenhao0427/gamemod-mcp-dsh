#!/usr/bin/env python3
"""验证：同步工具会占住 MCP 事件循环，期间服务器对**任何**请求都不回话。

为什么要重做：上一版用 `open_game(force_rebuild=True)` 当慢调用，实测 0.06s
就返回 —— 那是**空转**，什么都没测到（结论无效，脚本自己也打印了
「本轮无效」，这个设计是对的）。慢调用必须真的慢。

本轮改成：**全新缓存目录**的 IL2CPP 建索引。实测 Pass The Fear 要 2.4 秒
（26,535 类型 / 37 MB 库），足够覆盖并发探测窗口。

判据（两条都要成立才算证据）：
  ① 慢调用确实慢（≥2s）；
  ② 期间 `get_session`（**不碰 Frida**、只读内存里的会话状态）拿不到响应。

代码层面依据（读 mcp 1.26.0 源码，非推测）：
  FastMCP 的 FunctionTool.run → func_metadata.call_fn_with_arg_validation
  对同步函数是直接 `return fn(**kwargs)`，**没有** to_thread。

用法：
    python .cache-test\blocking_check.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

# 仓库根**自寻**（理由见 runtime_matrix.py 的说明）：不写死 .parent 层数。
def _repo_root() -> Path:
    for p in Path(__file__).resolve().parents:
        if (p / "src" / "gamemod_mcp" / "server.py").is_file():
            return p
    raise SystemExit("找不到仓库根（没有 src/gamemod_mcp/server.py）")


ROOT = _repo_root()
PY = r"C:\Users\temp\AppData\Local\Programs\Python\Python310\python.exe"
SERVER = ROOT / "src" / "gamemod_mcp" / "server.py"
GAME = r"E:\project\Pass The Fear"
FRESH = ROOT / ".cache-test" / "blocking-cache"


class Bare:
    """裸 JSON-RPC：必须能**并发**发请求，所以不能用顺序读的 mcp_stdio。"""

    def __init__(self) -> None:
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src"),
               "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1",
               "GAMEMOD_LOG_LEVEL": "WARNING"}
        self.p = subprocess.Popen(
            [PY, str(SERVER)], env=env, cwd=str(ROOT),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8",
            errors="replace", bufsize=1)
        self.replies: dict[int, dict] = {}
        self.lock = threading.Lock()
        self.nid = 0
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._sink_err, daemon=True).start()

    def _read(self) -> None:
        for line in self.p.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                m = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(m, dict) and m.get("id") is not None:
                with self.lock:
                    self.replies[m["id"]] = m

    def _sink_err(self) -> None:
        for _ in self.p.stderr:
            pass

    def send(self, method: str, params: dict) -> int:
        with self.lock:
            self.nid += 1
            rid = self.nid
        self.p.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "id": rid, "method": method,
             "params": params}) + "\n")
        self.p.stdin.flush()
        return rid

    def wait(self, rid: int, timeout: float):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.lock:
                if rid in self.replies:
                    return self.replies.pop(rid)
            time.sleep(0.02)
        return None

    def call(self, name: str, args: dict, timeout: float):
        return self.wait(self.send("tools/call",
                                   {"name": name, "arguments": args}), timeout)

    def request(self, method: str, params: dict, timeout: float):
        """任意 JSON-RPC 方法（initialize 走这里）。

        为什么要分开：`call` 固定发 `tools/call`。上一版脚本用 `call("initialize",
        ...)` 其实是「调用一个叫 initialize 的工具」，服务器如实回了
        `-32602 Invalid request parameters` —— 那个错误被当成「无响应」，
        于是整轮实验变成了空转。
        """
        return self.wait(self.send(method, params), timeout)

    def close(self) -> None:
        try:
            self.p.stdin.close()
            self.p.wait(timeout=10)
        except Exception:
            self.p.kill()


def unwrap(msg) -> dict:
    """从 JSON-RPC 响应里取出工具返回的 dict；**并区分三种失败**。

    上一版把「错误响应」和「超时（None）」都当成「无响应」，于是
    `-32602 Invalid request parameters` 被误读成「服务器被阻塞」。
    这里必须分开：error 要打出来，不能静默当成超时。
    """
    if not msg:
        return {}
    if "error" in msg:
        raise RuntimeError(f"JSON-RPC 错误：{msg['error']}")
    res = (msg.get("result") or {})
    sc = res.get("structuredContent")
    if isinstance(sc, dict):
        return sc
    txt = (res.get("content") or [{}])[0].get("text", "")
    try:
        return json.loads(txt) if txt else {}
    except json.JSONDecodeError:
        return {}


def main() -> int:
    if FRESH.exists():
        shutil.rmtree(FRESH)
    c = Bare()
    hello = c.request("initialize",
                      {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "bc", "version": "1"}}, 60)
    if not hello or "error" in hello:
        print(f"initialize 失败：{hello}")
        return 1
    print("initialize ok")

    slow_box: dict = {}

    def slow():
        t0 = time.time()
        m = c.call("open_game", {"game_dir": GAME, "cache_dir": str(FRESH)},
                   600)
        slow_box["seconds"] = round(time.time() - t0, 2)
        d = unwrap(m)
        slow_box["ok"] = d.get("ok")
        slow_box["types"] = (d.get("index") or {}).get("type_count")

    # 先探一次基线（此时没有慢调用在飞）
    t0 = time.time()
    base = unwrap(c.call("get_session", {}, 20))
    print(f"基线 get_session: {'ok' if base else '无响应'} "
          f"({time.time()-t0:.2f}s)")

    th = threading.Thread(target=slow)
    th.start()
    time.sleep(0.3)          # 让慢调用确实开始（建索引 2.4s，够）
    print("慢调用（全新缓存 → 真建索引）已在飞，并发探测 get_session…")
    probes = []
    while th.is_alive() and len(probes) < 10:
        t0 = time.time()
        got = c.call("get_session", {}, 1.5)
        dt = round(time.time() - t0, 2)
        probes.append((dt, bool(got)))
        print(f"  探测#{len(probes)}: {'秒回' if got else '**1.5s 内无响应**'} "
              f"({dt}s)")
    th.join(timeout=620)

    t0 = time.time()
    after = unwrap(c.call("get_session", {}, 20))
    print(f"\n慢调用: {slow_box.get('seconds')}s ok={slow_box.get('ok')} "
          f"类型={slow_box.get('types')}")
    print(f"结束后 get_session: {'ok' if after else '无响应'} "
          f"({time.time()-t0:.2f}s)")

    real = float(slow_box.get("seconds") or 0) >= 2.0
    blocked = [p for p in probes if not p[1]]
    print("\n判定:")
    print(f"  ① 慢调用确实慢（≥2s，非空转）: {real} "
          f"（{slow_box.get('seconds')}s）")
    print(f"  ② 期间 get_session 无响应: {len(blocked)}/{len(probes)} 次")
    if real and blocked:
        print("  → **阻塞成立**：慢调用期间服务器无法响应任何其它请求。"
              "get_session 不碰 Frida，只读内存里的状态，"
              "它被挡住只能是事件循环被同步调用占住。")
    elif not real:
        print("  → **本轮无效**：慢调用没真慢下来")
    else:
        print("  → 阻塞不成立，需要推翻上面的源码解释")
    c.close()
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

    log.write(f"\n##### {time.strftime('%H:%M:%S')} #####\n")
    log.flush()
    sys.stdout = _T(sys.stdout, log)
    try:
        raise SystemExit(main())
    finally:
        log.flush()
