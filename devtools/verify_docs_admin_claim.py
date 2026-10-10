"""验收：文档里**不许**再出现「注入需要管理员权限」这个错误说法。

为什么需要一条专门的断言
============================================================
这条错误说法在仓库里存在了很久，而且**扩散到了 8 处以上**
（`session.py` 的失败提示、`server.py` 的 instructions 与 docstring、
README 两处、SKILL.md 两处、`install.ps1`、当时的分发脚本）。

它的来源是 `session.py` 里 `inject_process` 失败时的一句**猜测性归因**
（「权限不足（需以管理员身份运行）」）—— 一句话的猜测被当成了普遍规律，
然后被复制到各处。而事实是：

    实测（2026-10）：普通用户权限（完整性级别 Medium）注入同样普通权限的
    IL2CPP 游戏，attach → runtime_info → find_instances **全部成功**。

正确表述是「**权限级相同即可**」：游戏是管理员运行时（High）才需要提权。

为什么这条要比一般文档断言更严
============================================================
**把错误结论写成门禁，比没有门禁更糟** —— 它会阻止别人改对。
本项目就踩过：当时的分发脚本里曾有一条
`check("一句话安装.md 说明了管理员权限要求", "管理员" in t)`，
它**要求**产物里出现这个错误说法。所以本脚本同时断言两件事：

  1. 文档里不出现「必须/需要管理员」这类**无条件**说法；
  2. 文档里**有**「不需要管理员」+ 权限级例外的正确表述。

第 2 条是关键：只检查「不出现错误说法」的话，把所有相关内容删光
也能通过 —— 那是另一种形式的丢失信息。

本脚本纯静态扫文本，不需要游戏、不需要注入，永远会跑。
"""

from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import samples                                            # noqa: E402
samples.add_src_to_path()
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = samples.ROOT

PASS = 0
FAIL = 0
WARN = 0


def check(label: str, cond: bool, detail: str = "", warn_only: bool = False) -> bool:
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
    print("=" * 78)
    print(title)
    print("=" * 78)


#: 扫描范围：交付物里的文本文件。排除本机产物与历史归档。
SCAN_SUFFIX = (".md", ".py", ".ps1", ".sh", ".js", ".toml", ".txt")
#: ⚠️ **不能**把 `GameModMCP-share` 放进这里 —— 分享包里跑本脚本时，
#: ROOT 本身就是 `.../GameModMCP-share`，于是**每个**路径都含这个词，
#: 扫描结果变成 0 个文件，而「没有错误说法」这条会**静默通过**（空真）。
#: 实测在分享包里跑就是这个症状（0 个文件）。第 1 节的两条非空转断言
#: 正是为了拦住这种「扫了个空集还说干净」。
SKIP_PARTS = ("__pycache__", ".git", ".cache", ".cache-test",
              ".cache-adapt-test", ".cache-lazy-test", ".cache-matrix",
              ".cache-verify", "agent_logs", "groundtruth", "archive")


def scan_files() -> list:
    out = []
    for p in ROOT.rglob("*"):
        if not p.is_file() or p.suffix not in SCAN_SUFFIX:
            continue
        if any(part in SKIP_PARTS for part in p.parts):
            continue
        # 本脚本自身会引用这些错误说法作为「反例」，跳过；
        # 一次性侦察脚本 `_probe_*.py` 也会把错误说法当**背景**引用
        # （「文档里长期写着 X」），那是记录历史，不是断言，也跳过
        # —— 按 .gitignore，它们本来就不入库。
        if p.name == "verify_docs_admin_claim.py" or p.name.startswith("_probe_"):
            continue
        out.append(p)
    return sorted(out)


#: 「无条件要求管理员」的错误说法。
#: 注意要**排除**正确表述（含「不需要」「不是」「例外」「权限级」等上下文），
#: 否则会把订正后的句子也误判。
BAD_PATTERNS = [
    # 「需要/要 管理员权限」「需以管理员身份运行」
    re.compile(r"需要\s*\*{0,2}管理员"),
    re.compile(r"需以管理员身份运行"),
    # ⚠️ 下面这条是**反证逼出来的**：第一版只写了 `通常…需要管理员`，
    # 结果漏掉了 SKILL.md 里**真实存在**的那句
    # 「Windows 上通常要**管理员身份**运行 MCP 服务器」
    # ——「通常要管理员」里没有「需要」二字。
    # 教训：反证必须用**真实出现过的原句**，不能只用自己造的样本。
    re.compile(r"通常\s*\*{0,2}要\s*\*{0,2}管理员"),
    re.compile(r"要\s*\*{0,2}管理员身份\s*\*{0,2}运行"),
    re.compile(r"请用管理员身份启动"),
    re.compile(r"\*\*Windows 注入通常需要管理员权限\*\*"),
]
#: 出现这些词就认为该行是**正确表述**（在纠正/限定），不算错误。
OK_HINTS = ("不需要", "不用", "不是", "无需", "例外", "权限级", "完整性级别",
            "曾经写错", "订正", "错误", "不对", "仅当", "只有游戏",
            "才需要", "才需要提权", "High",
            # 注释里说明「以前那样写是错的」也算纠正语境
            "不再", "不该", "不许", "禁止")


def line_is_bad(line: str) -> bool:
    """判断一行是否属于「无条件要求管理员」的错误说法。

    两个必要的豁免（都是**引用**而非**断言**，不构成错误信息）：

      · 行内出现「不需要/不是/例外/订正」等限定词 → 是在纠正，不是断言；
      · 行内出现 `not in` / `"..." not in` → 是**反向断言的代码**
        （`build_share.py` 里有 `check(..., "需要管理员权限" not in t)`，
        那条代码正是在阻止这个说法，不该被判成错误）。
    """
    low = line
    if any(h in low for h in OK_HINTS):
        return False
    # 反向断言的代码行：`xxx not in t`
    if "not in" in low:
        return False
    # **检测器自身的代码**：`build_share.py` 里有一段 `_bad_admin_line()`
    # 专门用来揪这个错误说法，它的判断条件里必然含这些字面量
    # （`"需要管理员" in line or ...`）。那是**判据**不是**断言**，
    # 与上一行的 `not in` 是同一类。用 `in line` 这个形态识别它。
    if re.search(r'["\']需要管理员["\']\s+in\s+', line):
        return False
    if re.search(r'["\']要管理员["\']\s+in\s+', line):
        return False
    return any(p.search(line) for p in BAD_PATTERNS)


files = scan_files()

# ================================================================
section("1. 不许出现「注入需要管理员权限」的无条件说法")

bad_hits = []
for p in files:
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        continue
    for i, line in enumerate(text.splitlines(), 1):
        if line_is_bad(line):
            bad_hits.append(f"{p.relative_to(ROOT)}:{i}: {line.strip()[:100]}")

# 非空转保证：确认扫到了文件、且这些文件里确实有「管理员」字样
check("扫到了待检查的文件（否则下面的断言是空转的）",
      len(files) > 50, f"{len(files)} 个文件")
has_admin = 0
for p in files:
    try:
        if "管理员" in p.read_text(encoding="utf-8", errors="replace"):
            has_admin += 1
    except Exception:
        pass
check("确实有文件提到「管理员」（证明扫描不是扫了个空集）",
      has_admin >= 3, f"{has_admin} 个文件提到")

check("没有「无条件要求管理员」的错误说法", not bad_hits,
      "\n      ".join(bad_hits[:6]) if bad_hits else "（干净）")

# ================================================================
section("2. 必须**正面**给出正确说法（只删错误说法 = 丢信息）")

# 关键是 SKILL.md 与 README —— Agent 与用户真正读的两份。
# 另外两处是**代码里的用户可见提示**：session.py 是错误说法的源头，
# install.py 是安装时打印给用户看的（也会进分享包）。
skill = ROOT / "skills" / "gamemod-mod" / "SKILL.md"
readme = ROOT / "README.md"
session = ROOT / "src" / "gamemod_mcp" / "runtime" / "session.py"
server = ROOT / "src" / "gamemod_mcp" / "server.py"
install_py = ROOT / "install.py"

for path, label in ((skill, "SKILL.md"), (readme, "README.md"),
                    (session, "session.py"), (server, "server.py"),
                    (install_py, "install.py")):
    if not path.is_file():
        check(f"{label} 存在", False, "文件不见了")
        continue
    t = path.read_text(encoding="utf-8")
    check(f"{label} 含「不需要管理员」的正面表述",
          "不需要管理员" in t,
          "只删掉错误说法、不留正确说法 = 丢信息")
    check(f"{label} 说明了「权限级相同」或提权例外",
          ("权限级" in t or "完整性级别" in t or "以管理员身份运行" in t
           or "管理员运行" in t),
          f"{label} 应解释唯一需要提权的情形")

# ================================================================
section("3. 失败提示不许再拿「权限」当第一归因（根因就在这）")

if session.is_file():
    t = session.read_text(encoding="utf-8")
    # attach 失败那段
    m = re.search(r"注入 pid=\{pid\} 失败：\{exc\}。.*?\"\s*\)\s*from exc",
                  t, re.S)
    seg = m.group(0) if m else ""
    check("session.py 的 attach 失败提示不再断言「需管理员」",
          "需以管理员身份运行" not in seg,
          "这是那条错误说法扩散的源头")
    check("session.py 的失败提示给出「权限级不匹配」这个准确原因",
          "权限级不匹配" in seg or "权限级" in seg,
          "换成准确原因，而不是删掉了事")

if server.is_file():
    t = server.read_text(encoding="utf-8")
    check("server.py 的 inject_process docstring 说明「不需要管理员」",
          "不需要管理员" in t)
    check("server.py 的 instructions 不再写「通常需要管理员」",
          "通常需要管理员" not in t)

# ================================================================
section("4. 分发层（DSH 组合包）不许把错误说法锁进产物")

# 分发方式已从「打分享包 zip」改为「DSH 组合包」：
# 对方在 DSH 里安装本包即接入，安装说明由组合包自己携带。
# 这一节相应改为检查**新的**分发产物。
#
# ⚠️ 非空转保证：这里显式断言这些文件**存在**。
# 上一版检查的是 `build_share.py`，它被删除后整节因为 `if is_file()`
# 而**静默跳过** —— 「检查过了」与「什么都没检查」输出一样。
# 这是本项目反复踩到的坑（空真），所以先断言被检对象在。
for rel in ("package.json", "cordis.patch.yml", "index.js"):
    check(f"分发产物存在：{rel}", (ROOT / rel).is_file(),
          "被检对象不存在时整节会静默跳过（空真），必须先断言它在")

patch = ROOT / "cordis.patch.yml"
if patch.is_file():
    t = patch.read_text(encoding="utf-8")
    check("cordis.patch.yml 不写死绝对路径",
          not re.search(r"[A-Za-z]:[\\/]", t),
          "写死路径会让包在别人机器上失效；解释器与路径应由 Host 插件运行时算出")
    check("cordis.patch.yml 用 inject 保证「先装依赖再拉服务器」",
          "inject:" in t and "gamemodToolchain" in t,
          "Loader 并发挂载各行，行序不保证顺序；顺序必须由依赖关系表达")

idx = ROOT / "index.js"
if idx.is_file():
    t = idx.read_text(encoding="utf-8")
    # 反向断言：把错误结论写成门禁比没有门禁更糟
    check("index.js 没有「要求出现管理员权限」的旧式断言",
          "管理员权限要求" not in t)
    check("index.js 的依赖约束带 <2（mcp 2.x 会起不来）",
          "mcp>=1.0.0,<2" in t,
          "只写 >=1.0.0 会让干净环境装到 2.x，FastMCP 已改名 MCPServer")

# ================================================================
section("5. 非空转反证：把错误说法塞回去，断言必须报红")

# 说明：本节的判据函数 line_is_bad 就是第 1 节用的那个，
# 这里用**人造样本**证明它真的会抓。
_samples_bad = [
    "注入需要管理员权限",
    "Windows 上通常要管理员身份运行 MCP 服务器",   # ← 真实出现过的那句
    "需以管理员身份运行",
    "请用管理员身份启动你的 Agent",
    "注入游戏进程通常需要管理员权限",
]
_samples_ok = [
    "注入游戏进程**不需要管理员权限**（实测订正）。",
    "只有游戏以管理员身份运行时才需要同样提权。",
    "真正的失败条件是**权限级不匹配**。",
    "这条曾经写错成「通常需要管理员权限」。",
]
for s in _samples_bad:
    check(f"判据会把错误说法判为「坏」：{s[:36]}", line_is_bad(s))
for s in _samples_ok:
    check(f"判据不会误伤正确表述：{s[:36]}", not line_is_bad(s))

# 反证总结：如果判据恒假（永不报红），上面 4 条会全红
_all_bad_caught = all(line_is_bad(s) for s in _samples_bad)
check("反证成立：判据不是恒假（4 条错误样本全被抓到）", _all_bad_caught)

# ================================================================
print()
print("=" * 78)
print(f"通过 {PASS} 项，失败 {FAIL} 项" + (f"，警告 {WARN} 项" if WARN else ""))
print("=" * 78)
sys.exit(1 if FAIL else 0)
