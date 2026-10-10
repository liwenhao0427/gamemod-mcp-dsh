#!/usr/bin/env python3
"""极简 stdio MCP 客户端：验证脚本共用的「起服务器 + 握手 + 调工具」底座。

为什么自己写一个，而不是用官方的 `mcp.client.stdio`
============================================================
两者定位不同，都需要：

- `devtools/report_multi_game_matrix.py` 用**官方 SDK 客户端** ——
  它扮演的是真实 Agent，测试的是「按 SDK 正常接入能不能用」。
- 本模块用**裸 JSON-RPC** —— 它多验两件官方客户端会替我们掩盖的事：
  ① stdout 里**只有** JSON-RPC（官方客户端会容忍/自己解析，坏了也未必报错）；
  ② 服务器**起不来**时，错误原因能完整带回来（官方客户端把子进程 stderr
     丢掉，只剩一句 `McpError: Connection closed`）。

实测价值：`server.py` 的「以脚本方式启动」曾经是坏的，
官方客户端只报 `Connection closed`，而本模块直接把子进程 stderr 末尾的
`ImportError: attempted relative import with no known parent package` 带了出来。

读线程 + 队列
============================================================
stdout / stderr 各起一个读线程，主线程只从队列取。这样：
- 任意等待都有超时，服务器卡死不会把测试脚本一起拖住；
- stderr 一直有人读，不会因为管道写满把子进程堵死（64 KB 缓冲区很快就满，
  而本项目的服务器会往 stderr 打日志）。
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from collections import deque


class ServerGone(RuntimeError):
    """服务器进程提前退出 / 不再回话。"""


class McpStdioServer:
    """一个 stdio MCP 服务器进程 + 裸 JSON-RPC 客户端。"""

    def __init__(self, cmd: list[str], env: dict | None = None,
                 cwd: str | None = None):
        self.cmd = cmd
        self.cwd = cwd
        self.env = {**os.environ} if env is None else dict(env)
        self._proc: subprocess.Popen | None = None
        self._q: queue.Queue = queue.Queue()
        self._err: deque[str] = deque(maxlen=60)
        self._next_id = 0
        #: stdout 里出现过的非 JSON 行数（协议污染，正常必须为 0）
        self.non_json_lines = 0
        #: 污染样本（前 8 条原文，便于定位是谁在往 stdout 写）
        self.bad_lines: list[str] = []

    # ------------------------------------------------------------ 生命周期

    def start(self) -> "McpStdioServer":
        self._proc = subprocess.Popen(
            self.cmd, cwd=self.cwd, env=self.env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8",
            errors="replace", bufsize=1,
        )
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()
        return self

    def _pump_stdout(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        for line in self._proc.stdout:
            self._q.put(line)
        self._q.put(None)            # 结束哨兵

    def _pump_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        for line in self._proc.stderr:
            self._err.append(line.rstrip())

    def close(self) -> None:
        if self._proc is None:
            return
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
            self._proc.wait(timeout=10)
        except Exception:            # noqa: BLE001 - 收尾失败不该让测试崩
            self._proc.kill()

    def __enter__(self) -> "McpStdioServer":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------ 通信

    def stderr_tail(self, n: int = 12) -> str:
        return "\n".join(list(self._err)[-n:])

    def _send(self, obj: dict) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise ServerGone("服务器未启动")
        try:
            self._proc.stdin.write(json.dumps(obj) + "\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, ValueError) as exc:
            raise ServerGone(f"写入 stdin 失败：{exc}") from exc

    def _read_until(self, want_id: int, timeout: float) -> dict:
        deadline = time.time() + timeout
        while True:
            remain = deadline - time.time()
            if remain <= 0:
                raise ServerGone(f"等待 id={want_id} 的响应超时（{timeout}s）")
            try:
                line = self._q.get(timeout=remain)
            except queue.Empty:
                raise ServerGone(
                    f"等待 id={want_id} 的响应超时（{timeout}s）") from None
            if line is None:
                raise ServerGone("服务器 stdout 已关闭（进程提前退出？）")
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                # stdout 里出现非 JSON = 违反「stdout 只跑协议」这条硬规矩
                self.non_json_lines += 1
                if len(self.bad_lines) < 8:
                    self.bad_lines.append(line[:200])
                continue
            if not isinstance(msg, dict):
                # 合法 JSON 但**不是** JSON-RPC 消息（例如被启动的游戏往同一个
                # stdout 里打了 `"..."`）—— 同样是协议污染。
                # 这里以前会直接 `msg.get` 抛 AttributeError，把「被测游戏
                # 污染了协议流」这个真问题伪装成脚本自己的 TypeError。
                self.non_json_lines += 1
                if len(self.bad_lines) < 8:
                    self.bad_lines.append(line[:200])
                continue
            if msg.get("id") == want_id:
                return msg

    def _request(self, method: str, params: dict | None,
                 timeout: float) -> dict:
        self._next_id += 1
        rid = self._next_id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method,
                    "params": params or {}})
        return self._read_until(rid, timeout)

    def notify(self, method: str, params: dict | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    # ------------------------------------------------------------ 高层接口

    def initialize(self, timeout: float = 60.0) -> dict:
        """握手。返回 {ok, error, server_info, tools}（tools 为 None 表示没查）。"""
        out: dict = {"ok": False, "error": "", "server_info": {}, "tools": None}
        try:
            msg = self._request("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "gamemod-devtools", "version": "1"},
            }, timeout)
        except ServerGone as exc:
            out["error"] = f"{exc}；stderr 末尾：\n{self.stderr_tail()}"
            return out
        if "error" in msg:
            out["error"] = f"initialize 返回错误：{msg['error']}"
            return out
        out["server_info"] = msg.get("result", {}).get("serverInfo", {})
        out["ok"] = True
        try:
            self.notify("notifications/initialized")
        except ServerGone:
            pass
        return out

    def tools(self, timeout: float = 60.0) -> dict:
        """tools/list。返回 {ok, error, names}。"""
        out: dict = {"ok": False, "error": "", "names": []}
        try:
            msg = self._request("tools/list", {}, timeout)
        except ServerGone as exc:
            out["error"] = f"{exc}；stderr 末尾：\n{self.stderr_tail()}"
            return out
        if "error" in msg:
            out["error"] = f"tools/list 返回错误：{msg['error']}"
            return out
        out["names"] = [t["name"] for t in msg.get("result", {}).get("tools", [])]
        out["ok"] = True
        return out

    def call_tool(self, name: str, arguments: dict | None = None,
                  timeout: float = 300.0) -> dict:
        """调一个工具。

        Returns:
            {ok, is_error, data, text, error}
            `ok` 指**这次调用本身**有没有走通（不是工具返回的 `ok` 字段）；
            工具内部的业务失败是 `data={"ok": False, ...}`，
            而 `is_error=True` 表示工具抛异常（例如 ImportError）。
        """
        out: dict = {"ok": False, "is_error": False, "data": None,
                     "text": "", "error": ""}
        try:
            msg = self._request("tools/call",
                                {"name": name, "arguments": arguments or {}},
                                timeout)
        except ServerGone as exc:
            out["error"] = f"{exc}；stderr 末尾：\n{self.stderr_tail()}"
            return out
        if "error" in msg:
            out["error"] = f"tools/call 返回错误：{msg['error']}"
            return out
        res = msg.get("result", {})
        out["ok"] = True
        out["is_error"] = bool(res.get("isError"))
        sc = res.get("structuredContent")
        if isinstance(sc, dict):
            out["data"] = sc
        content = res.get("content") or []
        if content and isinstance(content[0], dict):
            out["text"] = content[0].get("text", "")
        if out["data"] is None and out["text"]:
            try:
                out["data"] = json.loads(out["text"])
            except json.JSONDecodeError:
                out["data"] = None
        if out["is_error"] and not out["error"]:
            out["error"] = out["text"][:400]
        return out


def script_form_env(src_dir: str, clean_pythonpath: bool = True) -> dict:
    """「以脚本方式启动」用的环境变量。

    Args:
        src_dir: 仓库的 src 目录
        clean_pythonpath: 是否清掉 PYTHONPATH。脚本形式**不该**依赖
            PYTHONPATH，所以默认清掉；否则「能跑」可能只是继承了环境变量，
            断言变成空转。
    """
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1",
           "GAMEMOD_LOG_LEVEL": "WARNING"}
    if clean_pythonpath:
        env.pop("PYTHONPATH", None)
    else:
        env["PYTHONPATH"] = src_dir
    return env


if __name__ == "__main__":
    # 直接运行 = 自检：起一次模块形式的服务器，打印握手与工具数
    sys.stdout.reconfigure(encoding="utf-8")
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    src = root / "src"
    srv = McpStdioServer([sys.executable, "-m", "gamemod_mcp.server"],
                         env=script_form_env(str(src), clean_pythonpath=False),
                         cwd=str(root)).start()
    try:
        init = srv.initialize()
        tl = srv.tools()
        print(f"initialize : ok={init['ok']} {init['server_info']} {init['error'][:200]}")
        print(f"tools/list : ok={tl['ok']} {len(tl['names'])} 个")
        print(f"stdout 非 JSON 行数: {srv.non_json_lines}")
        r = srv.call_tool("get_session", {})
        print(f"get_session: ok={r['ok']} is_error={r['is_error']} "
              f"engine={((r['data'] or {}).get('engine'))!r}")
    finally:
        srv.close()
