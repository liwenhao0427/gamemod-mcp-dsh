#!/usr/bin/env python3
"""验收：`launch_game` 启动的子进程**不许**把输出写进 MCP 服务器的 stdout。

为什么单独一个脚本
============================================================
`launch_game` 用 Frida 的 `device.spawn()` 起游戏。Frida 的 `stdio` 默认是
**`inherit`** —— 子进程直接继承父进程的 stdout，而 MCP 服务器的 stdout 是
**JSON-RPC 通道**（`AGENTS.md` §3.1 第 2 条）。

实测后果（Garden of Witches，启动瞬间）：

    [UnityMemory] Configuration Parameters - Can be set up in boot.config
    "memorysetup-bucket-allocator-granularity=16"
    "memorysetup-bucket-allocator-bucket-count=8"
    ... 共 30 行

其中 `"memorysetup-..."` 是**合法 JSON 字符串**：客户端 `json.loads` 会成功，
然后把一个 `str` 当 JSON-RPC 消息用 —— 实测把客户端的

    AttributeError: 'str' object has no attribute 'get'

伪装成「脚本自己的 bug」，排查方向完全被带偏。
（`devtools/mcp_stdio.py` 也因此加了「合法 JSON 但不是对象」的判别。）

修法：spawn 时传 `stdio="pipe"`，让 Frida 自己接住子进程输出，再经 device 的
`output` 信号转发到 **stderr**（`GAMEMOD_LOG_LEVEL=DEBUG` 可见）。
注意 Frida 17 的 `FridaStdio` 只有 `inherit` / `pipe`，**没有** `ignore`。

五节断言（含非空转反证）
============================================================
1. 代码里真的用的是 `pipe`（不是「文档说改了」）；
2. 反证：默认 `inherit` 时，子进程的标记**确实会**漏进父进程 stdout
   —— 证明第 3 节的检测不是空转；
3. `stdio="pipe"` 时，同一标记**不会**漏进父进程 stdout；
4. `stdio="pipe"` 不会把子进程卡死：写 4 MB（远超管道缓冲区）之后
   子进程必须还能继续跑（Frida 会排空管道）；
5. `mcp_stdio.py` 收到「合法 JSON 但不是对象」的行时，必须算作协议污染
   并继续等真正的响应，而不是抛 `AttributeError`。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import samples                                            # noqa: E402

samples.add_src_to_path()

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

PASS = 0
FAIL = 0
WARN = 0

MARKER = "NOISE-MARKER-12345"

#: 会往 stdout 打标记的子进程
NOISY = f'''
import sys, time
print("{MARKER}", flush=True)
time.sleep(1.0)
'''

#: 洪水子进程：先写 4 MB（远超任何管道缓冲区），再落标记文件。
#: 若 `stdio="pipe"` 之后没人排空管道，它会卡在 write 上，标记文件永不出现。
FLOOD = '''
import pathlib, sys, time
chunk = b"x" * 65536
for _ in range(64):
    sys.stdout.buffer.write(chunk)
    sys.stdout.buffer.flush()
pathlib.Path(sys.argv[1]).write_text("done", encoding="utf-8")
time.sleep(3)
'''


def check(label: str, cond: bool, detail: str = "",
          warn_only: bool = False) -> bool:
    global PASS, FAIL, WARN
    if cond:
        print(f"  \u2705 {label}" + (f" \u2014 {detail}" if detail else ""))
        PASS += 1
        return True
    if warn_only:
        print(f"  \u26a0\ufe0f  {label}" + (f" \u2014 {detail}" if detail else ""))
        WARN += 1
        return False
    print(f"  \u274c {label}" + (f" \u2014 {detail}" if detail else ""))
    FAIL += 1
    return False


def section(title: str) -> None:
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


def capture_stdout(fn) -> str:
    """把**本进程的 fd 1** 临时重定向到临时文件，跑 fn，返回捕获到的文本。

    为什么要重定向 fd 而不是替换 `sys.stdout`：子进程继承的是 **fd 1**，
    换掉 Python 的 `sys.stdout` 对象对子进程毫无影响，那样测出来必然是
    「没有泄漏」—— 一个永远为真的断言。
    """
    sys.stdout.flush()
    tmp = tempfile.TemporaryFile(mode="w+b")
    saved = os.dup(1)
    os.dup2(tmp.fileno(), 1)
    try:
        fn()
    finally:
        sys.stdout.flush()
        os.dup2(saved, 1)
        os.close(saved)
    tmp.seek(0)
    text = tmp.read().decode("utf-8", "replace")
    tmp.close()
    return text


def spawn_noisy(stdio: str | None) -> str:
    """spawn 一个会打标记的子进程，返回「漏进本进程 stdout」的内容。"""
    import frida

    helper = Path(tempfile.gettempdir()) / "gamemod_noisy_child.py"
    helper.write_text(NOISY, encoding="utf-8")
    dev = frida.get_local_device()
    kw: dict = {"cwd": str(helper.parent)}
    if stdio is not None:
        kw["stdio"] = stdio

    def _run() -> None:
        pid = dev.spawn([sys.executable, str(helper)], **kw)
        dev.resume(pid)
        time.sleep(1.8)
        try:
            dev.kill(pid)
        except Exception:  # noqa: BLE001
            pass

    return capture_stdout(_run)


def main() -> int:
    print("验收：launch_game 启动的子进程不许污染 MCP 的 stdout")
    print(f"仓库根: {samples.ROOT}")

    # ---------------------------------------------------------------- 1
    section("1. 产品代码真的用了 stdio='pipe'")
    from gamemod_mcp.runtime import session as sess  # noqa: E402

    check("session._SPAWN_STDIO == 'pipe'（不是默认的 inherit）",
          sess._SPAWN_STDIO == "pipe", f"实际={sess._SPAWN_STDIO!r}")
    src = (samples.SRC / "gamemod_mcp" / "runtime" / "session.py").read_text(
        encoding="utf-8")
    check("spawn 调用处真的把 stdio 传下去了（不是只定义了常量）",
          "stdio=_SPAWN_STDIO" in src, "session.py 里搜 'stdio=_SPAWN_STDIO'")
    check("转发子进程输出的回调挂在 device 上",
          'device.on("output"' in src, "device.on('output', ...)")

    # ---------------------------------------------------------------- 2/3
    section("2. 非空转反证：默认 inherit **确实**会漏（否则第 3 节没有意义）")
    leaked = spawn_noisy(None)
    print(f"     捕获到 {len(leaked.splitlines())} 行，"
          f"含标记={MARKER in leaked}")
    check("默认 stdio 下子进程输出漏进了父进程 stdout",
          MARKER in leaked, f"捕获内容前 80 字：{leaked[:80]!r}")

    section("3. stdio='pipe'：同一标记不再漏进父进程 stdout")
    leaked2 = spawn_noisy("pipe")
    print(f"     捕获到 {len(leaked2.splitlines())} 行，"
          f"含标记={MARKER in leaked2}")
    check("stdio='pipe' 下标记没有漏进来",
          MARKER not in leaked2, f"捕获内容前 80 字：{leaked2[:80]!r}")

    # ---------------------------------------------------------------- 4
    section("4. stdio='pipe' 不会把子进程卡死（4 MB 洪水必须写完）")
    import frida  # noqa: E402

    flood = Path(tempfile.gettempdir()) / "gamemod_flood_child.py"
    flood.write_text(FLOOD, encoding="utf-8")
    mark = Path(tempfile.gettempdir()) / "gamemod_flood_done.txt"
    mark.unlink(missing_ok=True)
    dev = frida.get_local_device()
    pid = dev.spawn([sys.executable, str(flood), str(mark)],
                    cwd=str(flood.parent), stdio="pipe")
    dev.resume(pid)
    time.sleep(8.0)
    check("子进程把 4 MB 写完了（说明 Frida 排空了管道）",
          mark.exists(), "没写完 = 子进程卡在 write 上，游戏会假死")
    try:
        dev.kill(pid)
    except Exception:  # noqa: BLE001
        pass

    # ---------------------------------------------------------------- 5
    section("5. 客户端侧：'合法 JSON 但不是对象' 必须算协议污染，不能崩")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import mcp_stdio  # noqa: E402

    srv = mcp_stdio.McpStdioServer.__new__(mcp_stdio.McpStdioServer)
    srv.non_json_lines = 0
    srv.bad_lines = []
    import queue as _q
    srv._q = _q.Queue()
    # 先喂两行「游戏日志」：一行合法 JSON 字符串、一行非 JSON；
    # 再喂真正的响应 —— 客户端必须跳过前两行、拿到响应。
    srv._q.put('"memorysetup-bucket-allocator-granularity=16"')
    srv._q.put("[UnityMemory] Configuration Parameters")
    srv._q.put(json.dumps({"jsonrpc": "2.0", "id": 7, "result": {"ok": True}}))
    got = srv._read_until(7, timeout=5.0)
    check("跳过污染行后仍拿到真正的响应",
          got.get("result", {}).get("ok") is True, str(got)[:80])
    check("两行污染都被计数", srv.non_json_lines == 2,
          f"实际={srv.non_json_lines}")
    check("污染原文被留证（便于定位是谁在写 stdout）",
          len(srv.bad_lines) == 2, f"{srv.bad_lines}")
    check("'合法 JSON 字符串' 这一行没有被当成消息用（不抛 AttributeError）",
          True, "上一行断言若能跑到这里就说明没抛")

    print()
    print("=" * 74)
    if FAIL:
        print(f"\u274c 通过 {PASS} 项，失败 {FAIL} 项（警告 {WARN}）")
    else:
        print(f"\u2705 通过 {PASS} 项，失败 0 项"
              + (f"（警告 {WARN}）" if WARN else ""))
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
