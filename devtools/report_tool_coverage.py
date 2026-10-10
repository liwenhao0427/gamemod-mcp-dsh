"""汇总动态覆盖率结果：把 .cache-test/tool_calls.jsonl 与工具清单对比。

这是 `verify_tool_coverage.py`（静态）的**动态**补充：
静态检查只证明工具名出现在脚本里，本脚本证明工具**真的被调用过**，
并给出每次调用的 `ok` 与返回体大小。

用法：
    python devtools/report_tool_coverage.py            # 只报告
    python devtools/report_tool_coverage.py --strict    # 有工具没被调用过则退出码非零

`--strict` 是用来兜住「验收脚本被放宽后偷偷丢掉覆盖」的：
某些断言（如 inject_process 的注入成功）因本机无显示环境只能记为警告，
一放宽就可能让某个工具再也没被真正跑到 —— 门禁要落在这里。
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict

_STRICT = "--strict" in sys.argv

# 中文 Windows 的控制台默认 GBK，本脚本会打印 ❌ / ⚠️ 之类的字符，
# 不做这一步会抛 UnicodeEncodeError（实测 '\u274c' codec can't encode）——
# 尤其致命的是**失败路径**：门禁本该列出「哪个工具没被覆盖」时自己崩掉，
# 把可操作的报错变成看不懂的编码错。
# 注意：`run_all_verify.py` **并不设** PYTHONIOENCODING，它只是用
# encoding="utf-8" 读子进程输出 —— 那是读，管不到本脚本**写** stdout。
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import samples  # noqa: E402

samples.add_src_to_path()

import gamemod_mcp.server as S  # noqa: E402

LOG = os.path.join(samples.ROOT, ".cache-test", "tool_calls.jsonl")
TOOLS = sorted(S.mcp._tool_manager._tools)

if not os.path.isfile(LOG):
    print(f"未找到调用日志：{LOG}")
    print("先按 README 的方式带垫片跑一遍验收脚本。")
    sys.exit(2)

stats: dict[str, dict] = defaultdict(lambda: {"n": 0, "ok": 0, "fail": 0,
                                              "max_size": 0, "errors": []})
with open(LOG, encoding="utf-8") as fh:
    for line in fh:
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        t = r.get("tool")
        if t not in TOOLS:
            continue
        s = stats[t]
        s["n"] += 1
        if r.get("ok") is True:
            s["ok"] += 1
        else:
            s["fail"] += 1
            if r.get("error"):
                s["errors"].append(str(r["error"])[:110])
        s["max_size"] = max(s["max_size"], int(r.get("size") or 0))

print("=" * 78)
print(f"工具动态覆盖率（日志 {os.path.relpath(LOG, samples.ROOT)}）")
print("=" * 78)

called = [t for t in TOOLS if stats[t]["n"] > 0]
never = [t for t in TOOLS if stats[t]["n"] == 0]

print(f"\n真实调用过 {len(called)} / {len(TOOLS)}\n")
print(f"{'工具':<26}{'调用':>6}{'ok':>6}{'非ok':>7}{'最大返回(B)':>13}")
print("-" * 78)
for t in TOOLS:
    s = stats[t]
    if s["n"] == 0:
        continue
    print(f"{t:<26}{s['n']:>6}{s['ok']:>6}{s['fail']:>7}{s['max_size']:>13,}")

if never:
    print(f"\n⚠️ 未被真实调用过（{len(never)} 个）：")
    for t in never:
        print(f"   {t}")
    print("\n   这说明验收脚本**没有真正跑到**这些工具，"
          "静态检查（verify_tool_coverage.py）看不出来。")

# 最大的几个返回体 —— 与「不返回超大结果」契约对照
print("\n返回体最大的 5 个工具（对照 AGENTS.md §3.1 第 5 条）：")
top = sorted((t for t in TOOLS if stats[t]["n"]),
             key=lambda t: -stats[t]["max_size"])[:5]
for t in top:
    kb = stats[t]["max_size"] / 1024
    print(f"   {t:<26}{kb:>10.1f} KB")

# 失败调用（可能合理：如故意传错参数的负例）
bad = [(t, s) for t, s in stats.items() if s["fail"]]
if bad:
    print("\n返回非 ok 的调用（含有意为之的负例）：")
    for t, s in sorted(bad):
        print(f"   {t:<26}{s['fail']} 次  例：{s['errors'][0] if s['errors'] else '-'}")

if _STRICT and never:
    print(f"\n❌ --strict：{len(never)} 个工具没有动态调用证据。")
    sys.exit(1)

sys.exit(0)
