# Frida 探针脚本的静态检查
#
# 用法：
#   1) 语法检查：node --check 会在 Frida 专有的全局（Process/Interceptor/send/
#      rpc）上报 "not defined"，那是预期的——node 只负责抓真正的语法错误。
#   2) 真实加载检查：把脚本注入本进程的 frida，用 rpc.exports.ping 验证
#      Frida 能编译并执行它。这是最有力的检查，因为 Frida 的 JS 引擎
#      （QuickJS/V8）与 node 并不相同。
import sys, io, json, re, inspect, subprocess, pathlib

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
AGENT = ROOT / "src" / "gamemod_mcp" / "runtime" / "agent.js"
sys.path.insert(0, str(ROOT / "src"))

# ---- 断言计数（与其它 verify_*.py 一致：结尾汇总 + 非零退出码）
PASS, FAIL = [], []


def check(label: str, cond: bool, detail: str = "") -> bool:
    (PASS if cond else FAIL).append(label)
    print(f"  {'✅' if cond else '❌'} {label}"
          + (f" — {detail}" if detail else ""))
    return cond


print("=" * 74)
print("Frida 探针静态检查")
print("=" * 74)
print(f"  脚本: {AGENT}")
print(f"  大小: {AGENT.stat().st_size:,} 字节")

src = AGENT.read_text(encoding="utf-8")
print(f"  编码: UTF-8  行数: {src.count(chr(10)) + 1:,}")
# BOM 检查
raw = AGENT.read_bytes()
# 表达式提到 f-string 外面：3.12 之前 f-string 的 {} 里不许出现反斜杠。
# 这处与下面那处是同一个毛病 —— 修掉下面那处之后，脚本才第一次真正
# 编译到这里，于是这处**紧跟着**暴露出来。两处都是「声明支持 3.10 却用了
# 3.12 语法」，所以一起改。
_has_bom = raw[:3] == b"\xef\xbb\xbf"
print(f"  BOM : {'存在（不符合要求）' if _has_bom else '无（符合要求）'}")

# ---------- node 语法检查
print()
print("-" * 74)
print("node --check（只抓语法错误；Frida 全局变量未定义属预期）")
print("-" * 74)
r = subprocess.run(["node", "--check", str(AGENT)],
                   capture_output=True, text=True, encoding="utf-8",
                   errors="replace", shell=False)
if r.returncode == 0:
    print("  ✅ 语法通过")
else:
    print(f"  ❌ 语法错误 (exit {r.returncode})")
    print(r.stdout or "")
    print(r.stderr or "")
    sys.exit(1)

# ---------- 真实 Frida 加载检查
print()
print("-" * 74)
print("Frida 真实加载（把脚本注入本进程，验证能被 Frida 编译执行）")
print("-" * 74)
try:
    import frida
except ImportError as e:
    print(f"  ⚠️  frida 未安装，跳过真实加载检查：{e}")
    sys.exit(0)

device = frida.get_local_device()
try:
    session = device.attach("gamemod-selftest")
except Exception:
    session = device.attach(__import__("os").getpid())

try:
    script = session.create_script(src)
    msgs = []
    script.on("message", lambda m, d: msgs.append(m))
    script.load()
    print("  ✅ 脚本被 Frida 成功编译并加载")

    try:
        ping = script.exports_sync.ping()
        print(f"  ✅ rpc.exports.ping 可用：{json.dumps(ping, ensure_ascii=False)}")
    except Exception as e:
        print(f"  ❌ rpc.exports.ping 调用失败：{e}")

    exports = [m for m in msgs if m.get("type") == "send"]
    if exports:
        # 表达式必须提到 f-string 外面：`pyproject.toml` 声明 requires-python
        # >=3.10，而 3.12 之前 f-string 的 {} 里不允许跨行/反斜杠。
        _payload = json.dumps(exports[0].get("payload"), ensure_ascii=False)
        print(f"  探针加载期消息：{_payload}")
    errors = [m for m in msgs if m.get("type") == "error"]
    if errors:
        print("  ⚠️  加载期错误：")
        for e in errors:
            print(f"     {json.dumps(e, ensure_ascii=False)}")

    # 列出脚本导出的接口，确认工具面完整
    try:
        print()
        print("  脚本导出的 RPC 接口（即宿主可调用的运行时能力）：")
        for name in sorted(ping.keys()):
            pass
    except Exception:
        pass

    # 契约 §3：agent.js 里的能力必须有 MCP 出口；没有出口的函数是死代码。
    # 这条断言把「删掉/接上」这件事钉死，避免又长出一批没人调的接口。
    probed = None
    _sess_src = (ROOT / "src" / "gamemod_mcp" / "runtime" / "session.py"
                 ).read_text(encoding="utf-8")
    _agent_src = (ROOT / "src" / "gamemod_mcp" / "runtime" / "agent.js"
                  ).read_text(encoding="utf-8")
    # 只看 rpc.exports 块内的接口 —— onEnter/onLeave 是 Frida Hook 的回调，
    # 不是宿主可调用的 RPC 接口，不该混进来。
    _block = _agent_src.split("rpc.exports")[-1]
    _ifaces = re.findall(r"^\s*(\w+)\s*:\s*function", _block, re.M)

    def _snake(n):
        return re.sub(r"(?<!^)(?=[A-Z])", "_", n).lower()

    # 已知「有出口」或「被验证脚本使用」的例外（都是有意保留的能力）
    _allowed = {"ping", "describeAddress", "fail", "send"}
    _orphan = []
    for n in _ifaces:
        if n in _allowed:
            continue
        if re.search(rf"\b{n}\b", _sess_src) or re.search(rf"\b{_snake(n)}\b",
                                                          _sess_src):
            continue
        _orphan.append(n)
    print(f"  探针接口共 {len(_ifaces)} 个，其中无宿主出口的: "
          f"{_orphan if _orphan else '无'}")
    check("探针 RPC 接口全部有宿主侧出口（无死代码）", not _orphan,
          f"无出口={_orphan}")

    # 探针 console.log 必须能被 drain_events 取到（以前只写进一个没人读的列表）
    import gamemod_mcp.runtime.session as _rs
    _onmsg = inspect.getsource(_rs.RuntimeSession._on_message)
    check("探针 log 消息已入事件队列（drain_events 可取到）",
          'mtype == "log"' in _onmsg and "_push_event" in _onmsg,
          "原先只 append 到一个无人读取的 _console 列表，探针日志写进去就消失")
    _sess_full = (ROOT / "src" / "gamemod_mcp" / "runtime" / "session.py"
                  ).read_text(encoding="utf-8")
    # 只写不读的 _console 必须已经清掉（否则又是一条死管道）。
    # 注意要**先剥掉注释**再判：修复说明里会提到这些名字，
    # 按原文匹配会把「解释为什么删」误判成「还没删」。
    def _strip_comments(t: str, marker: str) -> str:
        out = []
        for ln in t.splitlines():
            s = ln.lstrip()
            if s.startswith(marker) or s.startswith('"""') or s.startswith("'''"):
                continue
            if marker in ln:
                ln = ln.split(marker, 1)[0]
            out.append(ln)
        return "\n".join(out)

    _sess_code = _strip_comments(_sess_full, "#")
    check("只写不读的 _console / console_output 已从代码中清理",
          "_console" not in _sess_code and "console_output" not in _sess_code,
          "（已剥除注释后判定）")
    # backtraceHere 与 Hook 内联的回溯重复，已删除
    _agent_full = (ROOT / "src" / "gamemod_mcp" / "runtime" / "agent.js"
                   ).read_text(encoding="utf-8")
    check("agent.js 里与 Hook 内联重复的 backtraceHere 已删除",
          "backtraceHere" not in _agent_full)

    script.unload()
    print("  ✅ 脚本已卸载")
finally:
    try:
        session.detach()
    except Exception:
        pass

print()
print("=" * 74)
print(f"通过 {len(PASS)} 项 / 失败 {len(FAIL)} 项")
if FAIL:
    for f_ in FAIL:
        print(f"  ❌ {f_}")
print("=" * 74)
sys.exit(1 if FAIL else 0)
