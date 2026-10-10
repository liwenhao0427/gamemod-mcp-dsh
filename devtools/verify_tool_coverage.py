"""检查每个 MCP 工具是否被至少一个验收脚本**实际调用**过。

为什么需要这个
============================================================
`AGENTS.md` §5 要求「新增或修改工具时必须补/改对应的 MCP 层验收断言」，
但「有没有断言」这件事一直靠人检查 —— 而人检查必然漂移
（本项目已经吃过文档漂移的亏）。

本脚本把「工具覆盖率」变成可计算的量：遍历所有 verify_*.py，
找出每个工具名是否以**调用形式**出现（`name(` 或 `S.name(`），
未被调用的工具列出来，让人一眼看到验收的缺口。

注意这是**静态**检查，有两个已知的**高估**来源，别把它当成证明：
1. 只证明「脚本里出现了这个工具的调用形式」，不证明断言足够强。
2. `sess.hook_method(...)` 这类**会话对象**的调用也会被算进来
   （`sess` 是 RuntimeSession，不是 MCP 工具层），所以静态 38/38
   曾掩盖了 7 个工具其实没在 MCP 层真正跑过。

**真正的「跑过没有」要看动态证据**：
    python devtools\\report_tool_coverage.py
配合 `devtools/_cov/sitecustomize.py` 垫片使用，见 devtools/README.md。

所以本脚本是「补齐测试」的导航图，不是「测试已充分」的证明。

用法：
    python devtools/verify_tool_coverage.py            # 列出覆盖情况
    python devtools/verify_tool_coverage.py --strict    # 有未覆盖工具则退出码非零
"""

from __future__ import annotations

import os
import re
import sys

# 中文 Windows 的控制台默认 GBK，本脚本会打印 ✅ / ⚠️ 之类的字符，
# 不做这一步会抛 UnicodeEncodeError（'\u2705' codec can't encode）。
# 注意：`run_all_verify.py` **并不设** PYTHONIOENCODING（它只设
# PYTHONPATH 与 GAMEMOD_COV_OUT），所以这个坑在套件里同样会踩到。
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

_sys_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _sys_dir)

import samples  # noqa: E402

samples.add_src_to_path()

import gamemod_mcp.server as S  # noqa: E402

TOOLS = sorted(S.mcp._tool_manager._tools)

# 运行时类工具需要真注入游戏，脚本可能是「按环境跳过」的；
# 但本检查只看静态出现，跳过与否不影响。
verify_files = sorted(
    f for f in os.listdir(_sys_dir)
    if f.startswith("verify_") and f.endswith(".py")
)

# 汇总所有验收脚本的源码
corpus = {}
for fn in verify_files:
    try:
        with open(os.path.join(_sys_dir, fn), encoding="utf-8") as fh:
            corpus[fn] = fh.read()
    except OSError:
        pass

all_src = "\n".join(corpus.values())


def called_in(name: str) -> list[str]:
    """哪些脚本以调用形式用到了这个工具。"""
    # 匹配 name( 前面允许 S. / st. / state. 等，也允许行首
    pat = re.compile(r"(?<![\w.])" + re.escape(name) + r"\s*\(")
    hits = []
    for fn, src in corpus.items():
        if pat.search(src) or re.search(
            r"\.\s*" + re.escape(name) + r"\s*\(", src
        ):
            hits.append(fn)
    return hits


print("=" * 76)
print(f"MCP 工具验收覆盖检查（工具数 {len(TOOLS)}，验收脚本 {len(verify_files)} 个）")
print("=" * 76)

covered: dict[str, list[str]] = {}
missing: list[str] = []
for t in TOOLS:
    where = called_in(t)
    if where:
        covered[t] = where
    else:
        missing.append(t)

print(f"\n已覆盖 {len(covered)} / {len(TOOLS)}\n")
for t in TOOLS:
    if t in covered:
        who = "、".join(x.replace("verify_", "").replace(".py", "")
                        for x in covered[t][:3])
        more = f" 等{len(covered[t])}个" if len(covered[t]) > 3 else ""
        print(f"  ✅ {t:<26} ← {who}{more}")

if missing:
    print(f"\n未覆盖 {len(missing)} 个：")
    for t in missing:
        print(f"  ⚠️  {t}")
else:
    print("\n✅ 全部工具都有验收脚本调用")

strict = "--strict" in sys.argv
if missing and strict:
    print("\n→ --strict：存在未覆盖工具，退出码非零。")
    sys.exit(1)

print("\n注：本检查是静态的（工具名是否以调用形式出现），"
      "只用于定位验收缺口，不等于断言强度足够。")
sys.exit(0)
