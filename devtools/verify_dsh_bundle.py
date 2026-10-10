"""验收：DSH 组合包层（`index.js` / `cordis.patch.yml` / `package.json`）。

为什么需要一个**独立的**验收脚本
============================================================
MCP 工具层的 60 多个 `verify_*.py` 全部只覆盖 Python 侧。
新增的 DSH 组合包层（Host 插件 + patch + manifest）是**另一种语言、另一套
运行时**，Python 侧的脚本一行都碰不到它 —— 「Python 全套绿」与
「组合包根本装不上」可以同时成立。

这正是本项目反复强调的那条：**「仓库内全绿」不等于「Agent 看到的没问题」**。

覆盖什么
============================================================
分两层，缺一不可：

  · **静态**：manifest / patch 的形状对不对（有没有写死绝对路径、
    顺序是否由 `inject` 表达、依赖约束有没有 `<2`）；
  · **行为**：真的 `import` 那个模块并调用 `apply()`，用一个**假 ctx**
    观察它做了什么 —— 发布的服务字段是否齐全、`ctx.inject` 是否被用来
    做可选注入、skill 是否真的被注册、config 校验是否真的会拒绝坏输入。

第 2 层是关键：只读文本的话，`apply()` 里写错一个字段名也照样"通过"。

非空转反证
============================================================
脚本末尾用**真实文件**做反证：把 patch 改坏（写死路径 / 去掉 inject），
断言必须报红。见 `--counter-proof`。

用法
============================================================
    python devtools/verify_dsh_bundle.py
    python devtools/verify_dsh_bundle.py --counter-proof
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import samples                                            # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = samples.ROOT

#: 反证时用「另一份 patch 文件」而不是覆盖真文件。
#: 教训：本脚本第一版直接改写 `cordis.patch.yml` 再跑一次自己，
#: 结果被中断时**把产物本身改坏了**（留下一个写死路径的 patch）。
#: 验收脚本不该污染被测物 —— 改为用环境变量指向一份临时副本。
PATCH_OVERRIDE = os.environ.get("GAMEMOD_DSH_PATCH")

PASS = 0
FAIL = 0
WARN = 0


def norm(path) -> str:
    """把 Windows 路径统一成正斜杠，便于做后缀比较。"""
    return str(path).replace("\\", "/")


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


def node_available() -> bool:
    return shutil.which("node") is not None


def run_node(script: str, timeout: int = 300, tsx: bool = False,
             cwd: str | None = None) -> subprocess.CompletedProcess:
    """跑一段 ESM 脚本，返回结果。用临时文件避免命令行转义问题。

    @param tsx - 脚本要 import DSH 的 `.ts` 源码时必须开：Node 不认 TypeScript，
        需要 checkout 里的 `tsx` 加载器（与 DSH 自己的 `pnpm dsh` 一致）。
    @param cwd - 工作目录；import `.ts` 时必须是 DSH checkout（`tsx` 装在那里）。
    """
    cache = ROOT / ".cache-test"
    cache.mkdir(parents=True, exist_ok=True)
    fd, path = tempfile.mkstemp(suffix=".mjs", dir=str(cache))
    os.close(fd)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(script)
        argv = ["node"]
        if tsx:
            argv += ["--import", "tsx/esm"]
        argv.append(path)
        return subprocess.run(
            argv, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
            cwd=cwd or str(ROOT),
        )
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ================================================================
section("1. 组合包产物齐备（非空转：先确认被检对象存在）")

MANIFEST = ROOT / "package.json"
#: 反证时用 `GAMEMOD_DSH_PATCH` 指向一份**临时副本**，绝不改写真产物。
PATCH = Path(PATCH_OVERRIDE) if PATCH_OVERRIDE else ROOT / "cordis.patch.yml"
PLUGIN = ROOT / "index.js"
SKILL = ROOT / "skills" / "gamemod-mod" / "SKILL.md"

for label, path in [
    ("package.json", MANIFEST),
    ("cordis.patch.yml", PATCH),
    ("index.js", PLUGIN),
    ("skills/gamemod-mod/SKILL.md", SKILL),
    ("locale/en.json", ROOT / "locale" / "en.json"),
    ("locale/zh.json", ROOT / "locale" / "zh.json"),
    ("icon.svg", ROOT / "icon.svg"),
]:
    check(f"存在：{label}", path.is_file())

if FAIL:
    print("\n产物不齐，后续检查无意义。")
    sys.exit(1)

manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
patch_text = PATCH.read_text(encoding="utf-8")
plugin_text = PLUGIN.read_text(encoding="utf-8")

# ================================================================
section("2. manifest 声明了组合包（dsh.bundle.patch）")

check("name 是裸包名（不是相对路径）",
      isinstance(manifest.get("name"), str) and not manifest["name"].startswith("."),
      f"name={manifest.get('name')!r}")
check("声明 dsh.bundle.patch", "bundle" in manifest.get("dsh", {}),
      json.dumps(manifest.get("dsh"), ensure_ascii=False))
check("patch 指向存在的文件",
      (ROOT / manifest["dsh"]["bundle"]["patch"].lstrip("./")).is_file())
check("type 为 module（index.js 用 ESM）", manifest.get("type") == "module")
check("声明了 icon", manifest.get("icon") == "./icon.svg")
check("exports 暴露 package.json（readPluginMeta 需要读它）",
      "./package.json" in manifest.get("exports", {}))
check("exports 暴露 locale/*.json（展示元数据靠它）",
      any("locale" in k for k in manifest.get("exports", {})))
check("files 含 index.js 与 cordis.patch.yml",
      "index.js" in manifest.get("files", [])
      and "cordis.patch.yml" in manifest.get("files", []))

# 展示元数据必须放在 locale/*.json 的 meta 里 —— 实测 readPluginMeta
# 读的是 `${specifier}/locale/<lang>.json` 的 `meta.title/description`，
# 而**不是** package.json 的 meta 字段。放错地方不会报错，只是不显示。
check("展示元数据不在 package.json.meta（那里没人读）",
      "meta" not in manifest,
      "readPluginMeta 读 locale/*.json 的 meta；package.json.meta 是死字段")
for lang in ("en", "zh"):
    data = json.loads((ROOT / "locale" / f"{lang}.json").read_text(encoding="utf-8"))
    check(f"locale/{lang}.json 的 meta.title 非空",
          isinstance(data.get("meta", {}).get("title"), str)
          and data["meta"]["title"].strip() != "")
    check(f"locale/{lang}.json 的 meta.description 非空",
          isinstance(data.get("meta", {}).get("description"), str)
          and data["meta"]["description"].strip() != "")

# ================================================================
section("3. patch 的形状：顺序靠 inject，不靠行序")

#: 去掉注释行与空行后的**有效内容**。patch 文件带大段中文注释，
#: 直接对全文做 `startswith("-")` 会被注释挡住（第一版就是这么误报的）。
patch_body = "\n".join(
    line for line in patch_text.splitlines()
    if line.strip() and not line.lstrip().startswith("#")
)

check("patch 顶层是数组（loader 方言）", patch_body.lstrip().startswith("-"))
check("插入了两行（工具链 + MCP 客户端）",
      patch_text.count("id: gamemod-toolchain") == 1
      and patch_text.count("id: mcp-gamemod") == 1)
check("工具链行指向本包", re.search(r"id:\s*gamemod-toolchain\s*\n\s*name:\s*'gamemod-mcp'", patch_text) is not None)
check("MCP 行用官方 mcp-client", "@deepseek-ai/dsh-mcp-client" in patch_text)
# inject 支持两种写法：流式 `inject: [x]` 与块式 `inject:\n  - x`。
# 第一版只认块式，于是**正确**的流式写法被判红（假红）。
check("MCP 行 inject 了 gamemodToolchain（顺序保证）",
      re.search(r"inject:\s*\[\s*gamemodToolchain\s*\]", patch_text) is not None
      or re.search(r"inject:\s*\n\s*-\s*gamemodToolchain", patch_text) is not None,
      "Loader 并发挂载各行，行序不保证顺序")
check("解释器来自服务而非硬编码",
      "!!js ctx.gamemodToolchain.python" in patch_text)
check("args/cwd/env 全部来自服务",
      all(f"!!js ctx.gamemodToolchain.{f}" in patch_text
          for f in ("serverArgs", "root", "env")))
check("failOnStartupError: true（起不来要响亮地失败）",
      "failOnStartupError: true" in patch_text,
      "否则工具不见了却没人说为什么")
# 只在**有效内容**里查盘符：注释里提到路径（如举例说明）不算违规。
check("patch 里没有绝对路径（任何盘符）",
      re.search(r"[A-Za-z]:[\\/]", patch_body) is None,
      "写死路径会让包在别人机器上失效")
check("patch 里没有硬编码用户名/home",
      not re.search(r"(?i)C:\\+Users\\+[A-Za-z0-9_.-]+", patch_body))

# ================================================================
section("4. 依赖约束：mcp 必须 <2")

check("index.js 的 REQUIRED 含 mcp>=1.0.0,<2",
      "mcp>=1.0.0,<2" in plugin_text,
      "2.x 把 FastMCP 改名 MCPServer，导入路径变了")
check("index.js 的探测导入 mcp.server.fastmcp（1.x 路径）",
      "mcp.server.fastmcp" in plugin_text,
      "这是「服务器真能起来」的最小充分检查")
# 冷启动实测踩到过：`python -m venv` 返回 0、python.exe 也在，但紧接着
# `python -m pip` 报 No module named pip —— 而这是对方**第一次安装**必然
# 走的路径，一次偶发就等于装不上。所以必须显式引导 pip，不能假定它存在。
check("index.js 显式引导 pip（不假定 venv 建完就有）",
      "ensurepip" in plugin_text,
      "python -m venv 可能返回 0 但 pip 不可用（冷启动实测），必须有兜底")
# 顺序也是判据：把 ensurePip 放在 pipInstall **之后**等于没做
# （那时安装已经失败了）。这类「调了但没用」的缺陷只有查顺序才看得见。
#
# ⚠️ 必须匹配**调用语句**，不能只找 `ensurePip(python)` 这个子串 ——
# 函数定义 `function ensurePip(python) {` 里也含这个子串，而定义永远在
# 最前面，于是无论调用点在哪，断言都恒真。第一版就是这么写的，
# 非空转反证当场把它抓了出来（把调用挪到安装之后，断言**没有**报红）。
_call_ensure = re.search(r"^[ \t]+ensurePip\(python\)[ \t]*$", plugin_text, re.M)
_call_install = re.search(r"^[ \t]+const installed = pipInstall\(python, specs\)[ \t]*$",
                          plugin_text, re.M)
check("能找到 ensurePip 的调用语句", _call_ensure is not None)
check("能找到 pipInstall 的调用语句", _call_install is not None)
check("引导 pip 的调用在安装之前（否则等于没做）",
      _call_ensure is not None and _call_install is not None
      and _call_ensure.start() < _call_install.start(),
      f"ensurePip@{_call_ensure.start() if _call_ensure else None} "
      f"vs pipInstall@{_call_install.start() if _call_install else None}")
# 补不上 pip 时必须抛错，不许静默继续（否则会以「依赖装失败」的模糊
# 面目出现，而真正原因是 pip 本身不可用）。
check("pip 引导失败时抛错而不是静默继续",
      "且用 ensurepip 引导失败" in plugin_text)
# 冷启动实测踩到过一个**永久性**缺陷：venv 目录在、python.exe 也在，
# 但 pyvenv.cfg 是空的（venv 建到一半被打断），解释器一跑就报
# `No pyvenv.cfg file`。旧代码只查 `existsSync(python)` ⇒ 跳过创建 ⇒
# 直接装依赖 ⇒ 失败；而且**之后每次启动都看到 python.exe 在**，
# 于是永远不再重建 —— 一次被打断的首装就让这个包永久装不上。
check("index.js 用「解释器能跑」而不是「文件在不在」判断 venv",
      "function venvUsable" in plugin_text and "venvUsable(python)" in plugin_text,
      "只看 existsSync 会因半成品 venv 永久卡死（冷启动实测）")
check("index.js 会清掉不可用的 venv 残留再重建",
      "rmSync(venvDir" in plugin_text,
      "python -m venv 遇到非空目录会留下混合状态，必须先清")
req = (ROOT / "requirements.txt").read_text(encoding="utf-8")
check("requirements.txt 同样带 <2", "mcp>=1.0.0,<2" in req)
pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
check("pyproject.toml 同样带 <2", "mcp>=1.0.0,<2" in pyproject,
      "三处必须一致，否则按哪份装都可能装到 2.x")

# ================================================================
section("5. 行为：真的调用 apply()，用假 ctx 观察它做了什么")

if not node_available():
    check("node 可用（组合包是 JS）", False, "无 node 无法做行为验收")
else:
    (ROOT / ".cache-test").mkdir(exist_ok=True)
    probe = f'''
const mod = await import({json.dumps((ROOT / "index.js").as_uri())});
const logs = [];
const injected = [];
let provided = null;
let registered = null;
const ctx = {{
  logger: {{ info: m => logs.push("INFO " + m), warn: m => logs.push("WARN " + m) }},
  provide: (n, v) => {{ provided = {{ n, v }}; }},
  inject: (deps, cb) => {{
    injected.push(deps);
    cb({{ skills: {{ register: s => {{ registered = s; return () => {{}}; }} }} }});
  }},
}};
await mod.apply(ctx, {{}});
const out = {{
  provided,
  injected,
  registered: registered && {{
    name: registered.name,
    source: registered.source,
    provider: registered.provider,
    descriptionLength: registered.description.length,
    contentLength: registered.content.length,
    resourceBaseKind: registered.resourceBase && registered.resourceBase.kind,
    resourceBasePath: registered.resourceBase && registered.resourceBase.path,
    bodyStartsWithFrontmatter: registered.content.startsWith("---"),
  }},
  logs,
}};
console.log(JSON.stringify(out));
'''
    result = run_node(probe)
    if result.returncode != 0:
        check("apply() 能跑通", False,
              (result.stderr or result.stdout or "")[-600:])
    else:
        check("apply() 能跑通", True)
        try:
            data = json.loads(result.stdout.strip().splitlines()[-1])
        except Exception as exc:
            check("apply() 输出可解析", False, f"{exc}: {result.stdout[-300:]}")
            data = None

        if data:
            svc = data.get("provided")
            check("发布了服务", svc is not None and svc.get("n") == "gamemodToolchain",
                  f"name={svc.get('n') if svc else None!r}")
            v = (svc or {}).get("v", {})
            check("服务带 python（解释器路径）",
                  isinstance(v.get("python"), str) and v["python"] != "")
            check("服务带 serverArgs（含 -m gamemod_mcp.server）",
                  v.get("serverArgs") == ["-m", "gamemod_mcp.server"],
                  f"serverArgs={v.get('serverArgs')!r}")
            check("服务带 root（包根目录，供 cwd 用）",
                  isinstance(v.get("root"), str) and v["root"] != "")
            env = v.get("env", {})
            # ⚠️ 这里踩过一次：Python 源码里 `"\\\\"` 是**两个字符** `\\`，
            # 匹配不到单个反斜杠，于是**正确**的路径被判红（假红）。
            # 要替换一个反斜杠，字面量写 `"\\"`。
            check("服务带 env.PYTHONPATH（指向包内 src）",
                  isinstance(env.get("PYTHONPATH"), str)
                  and norm(env["PYTHONPATH"]).endswith("/src"),
                  f"PYTHONPATH={env.get('PYTHONPATH')!r}")
            check("env 设了 PYTHONUTF8=1（中文 Windows 控制台）",
                  env.get("PYTHONUTF8") == "1")
            check("env 设了 PYTHONDONTWRITEBYTECODE（不污染包目录）",
                  env.get("PYTHONDONTWRITEBYTECODE") == "1")
            check("服务带 venv 路径（供诊断）", isinstance(v.get("venv"), str))
            check("服务带 packages 清单", isinstance(v.get("packages"), list)
                  and len(v["packages"]) >= 2)

            check("用 ctx.inject 做**可选** skills 注入（缺 skills 也能工作）",
                  ["skills"] in data.get("injected", []),
                  "skills 不是必需依赖；用 inject 列表会让 MCP 在精简 profile 下不启动")

            reg = data.get("registered")
            check("真的注册了 skill", reg is not None)
            if reg:
                check("skill 名是 gamemod-mod", reg.get("name") == "gamemod-mod")
                check("skill source=bundled", reg.get("source") == "bundled")
                check("skill 有 description", reg.get("descriptionLength", 0) > 0)
                check("skill 正文非空", reg.get("contentLength", 0) > 1000,
                      f"contentLength={reg.get('contentLength')}")
                check("skill 正文已剥离 frontmatter",
                      not reg.get("bodyStartsWithFrontmatter"),
                      "frontmatter 应被解析掉，不当作正文发给模型")
                check("skill resourceBase 指向目录",
                      reg.get("resourceBaseKind") == "directory")
                check("resourceBase 指向 skill 目录",
                      norm(reg.get("resourceBasePath", "")).endswith("skills/gamemod-mod"))

    # ---- config 校验：坏输入必须被拒（不是静默忽略）----
    bad_cases = [
        ('{"unknownKey": 1}', "未知配置项"),
        ('{"vision": "yes"}', "vision 类型错"),
        ('{"python": 123}', "python 类型错"),
    ]
    for raw, label in bad_cases:
        bad_probe = f'''
const mod = await import({json.dumps((ROOT / "index.js").as_uri())});
const ctx = {{
  logger: {{ info: () => {{}}, warn: () => {{}} }},
  provide: () => {{}},
  inject: () => {{}},
}};
try {{
  await mod.apply(ctx, {raw});
  console.log(JSON.stringify({{ rejected: false }}));
}} catch (e) {{
  console.log(JSON.stringify({{ rejected: true, message: String(e && e.message || e) }}));
}}
'''
        r = run_node(bad_probe)
        try:
            out = json.loads(r.stdout.strip().splitlines()[-1])
            check(f"config 校验拒绝：{label}", out.get("rejected") is True,
                  "收下的参数必须真的影响行为；坏值应报错而不是静默忽略")
        except Exception:
            check(f"config 校验拒绝：{label}", False,
                  (r.stdout or r.stderr or "")[-300:])

# ================================================================
section("6. 行为：损坏的 venv 必须被自愈（不能永久卡死）")

# 这是冷启动实测抓到的**永久性**缺陷，所以单独做行为验收：
# 造一个真实的半成品 venv（目录在、python.exe 在、pyvenv.cfg 空），
# 看插件是「跳过创建然后一路失败」还是「清掉重建」。
#
# 只 grep `venvUsable` 是不够的 —— 那只能证明函数存在，不能证明它被用对。
# 真正的判据是：拿一个坏 venv 喂进去，它**修好了**。
if not node_available():
    check("node 可用（跑自愈行为测试）", False, warn_only=True)
else:
    import tempfile as _tf
    scratch = _tf.mkdtemp(prefix="gamemod-heal-", dir=str(ROOT / ".cache-test"))
    scratch_dsh = Path(scratch) / "dshhome"
    (scratch_dsh).mkdir(parents=True, exist_ok=True)
    broken_venv = scratch_dsh / "gamemod-mcp" / "venv"
    scripts = broken_venv / "Scripts"
    scripts.mkdir(parents=True, exist_ok=True)

    # 造半成品：**先建一个真的 venv，再把 pyvenv.cfg 清空**。
    #
    # 第一版是「拷一个 python.exe 过去、不写 pyvenv.cfg」，但那**造不出坏 venv**
    # —— 实测 rc=0（被拷过去的解释器会回退到基础安装，照跑不误），
    # 于是这一节测的是个正常环境，结论毫无意义（非空转前提当场报红）。
    # 真正复现实测形态的做法是：venv 建好之后把 pyvenv.cfg **清空**
    # —— 实测这会让解释器以 rc=107 报
    # `Cannot find home in '...pyvenv.cfg'`。
    base_python = shutil.which("python") or sys.executable
    made = subprocess.run([base_python, "-m", "venv", str(broken_venv)],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=600)
    if made.returncode != 0:
        check("能造出半成品 venv 用于测试", False,
              (made.stderr or made.stdout or "")[-300:], warn_only=True)
    else:
        # 清空 pyvenv.cfg —— 这就是实测看到的「建到一半被打断」形态。
        (broken_venv / "pyvenv.cfg").write_text("", encoding="utf-8")

    # 确认它确实是坏的（否则这个测试本身没有意义）
    probe_broken = subprocess.run(
        [str(scripts / "python.exe"), "-c", "print(1)"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=60,
    )
    check("测试前提成立：造出的 venv 确实不可用",
          probe_broken.returncode != 0,
          f"rc={probe_broken.returncode}（若为 0 说明没造坏，本测试无效）")

    heal = f'''
const mod = await import({json.dumps((ROOT / "index.js").as_uri())});
const logs = [];
const ctx = {{
  logger: {{ info: m => logs.push("INFO " + m), warn: m => logs.push("WARN " + m) }},
  provide: () => {{}},
  inject: () => {{}},
}};
try {{
  await mod.apply(ctx, {{}});
  console.log(JSON.stringify({{ ok: true, logs }}));
}} catch (e) {{
  console.log(JSON.stringify({{ ok: false, logs, error: String(e && e.message || e) }}));
}}
'''
    env = dict(os.environ)
    env["DSH_HOME"] = str(scratch_dsh)
    fd, nodefile = _tf.mkstemp(suffix=".mjs", dir=str(ROOT / ".cache-test"))
    os.close(fd)
    Path(nodefile).write_text(heal, encoding="utf-8")
    try:
        r = subprocess.run(["node", nodefile], capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           env=env, cwd=str(ROOT), timeout=1800)
        try:
            out = json.loads(r.stdout.strip().splitlines()[-1])
        except Exception:
            out = {"ok": False, "logs": [],
                   "error": (r.stdout or r.stderr or "")[-500:]}
        joined = "\n".join(out.get("logs", []))

        # ⚠️ 本节验的是「**坏 venv 被修好**」，不是「依赖装上了」。
        #
        # 两者要分开，否则这个脚本会依赖网络（pip install mcp+frida 约 40 MB）
        # —— 整套验收就变得不可离线复现了。而「修 venv」发生在装依赖**之前**，
        # 所以即使后面的安装因断网失败，venv 也已经被重建好，可以照常断言。
        healed = scratch_dsh / "gamemod-mcp" / "venv"
        healed_python = healed / "Scripts" / "python.exe"

        check("日志里说明它识别出了残留并重建",
              "残留" in joined or "重建" in joined,
              "没有这句说明它可能是碰巧成功的，而不是真的走了自愈分支")
        check("自愈后 pyvenv.cfg 存在且非空",
              (healed / "pyvenv.cfg").is_file()
              and (healed / "pyvenv.cfg").read_text(encoding="utf-8").strip() != "",
              "空 pyvenv.cfg 正是坏 venv 的形态（实测 rc=107）")
        # 这是本节的**核心判据**：重建出来的解释器真的能跑。
        # 旧代码只看 existsSync，会跳过重建、留下这个坏解释器。
        healed_ok = subprocess.run(
            [str(healed_python), "-c", "print(1)"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120,
        )
        check("自愈后的解释器真的能跑（旧代码会留下坏的那个）",
              healed_ok.returncode == 0,
              f"rc={healed_ok.returncode}")

        if out.get("ok") is True:
            check("依赖也装上了（网络可用）", True)
        else:
            # 装依赖失败**不算本节的失败** —— 它取决于网络，
            # 而本节的主张在上一行已经验完了。但要如实说出来。
            check("依赖安装（需要网络；本次未完成，不影响上面的自愈结论）",
                  False, str(out.get("error", ""))[:200], warn_only=True)
    finally:
        try:
            os.unlink(nodefile)
        except OSError:
            pass
        shutil.rmtree(scratch, ignore_errors=True)

# ================================================================
section("7. 组合包注册的 skill 必须盖过 ~/.dsh/skills/ 里的旧副本")

# 背景：旧机制是手工把 `skills/` 复制到 `~/.dsh/skills/`，而那份副本不在任何
# 门禁范围内 —— 实测曾落后 139 行，开头还教 Agent 去调一个**已删除**的工具。
# 新机制由组合包直接注册。但如果旧副本**优先级更高**，新机制就白做了：
# Agent 仍会读到旧的那份。所以「谁赢」必须被验证，不能靠推理。
#
# 规则（读自 DSH 源码）：候选按 rank **升序**排序，小者胜
# （`compareIndexedCandidates`）；runtime 注册的 rank 是 250，
# 而 skill-filesystem 的 user-dsh 根是 400 —— 所以组合包这份赢。
#
# 这一段需要 DSH 源码才能**实证**。没有就按 AGENTS.md §5.3 记为
# 「无法验证」并说出来，不许假装通过。
DSH_CHECKOUT = os.environ.get("DSH_CHECKOUT", r"E:\project\deepseek-harness")
dsh_skill_src = Path(DSH_CHECKOUT) / "packages" / "skill" / "skill" / "src" / "index.ts"
dsh_fs_src = Path(DSH_CHECKOUT) / "packages" / "skill" / "skill-filesystem" / "src" / "index.ts"

if not dsh_skill_src.is_file() or not dsh_fs_src.is_file():
    check(f"DSH 源码可用（{DSH_CHECKOUT}）", False,
          "无法验证优先级；这不是通过 —— 设 DSH_CHECKOUT 指向 DSH checkout 可启用本段",
          warn_only=True)
else:
    skill_src = dsh_skill_src.read_text(encoding="utf-8")
    fs_src = dsh_fs_src.read_text(encoding="utf-8")
    runtime_rank = int(re.search(r"RUNTIME_RANK\s*=\s*(\d+)", skill_src).group(1))
    user_rank = int(re.search(r"USER_DSH_RANK\s*=\s*(\d+)", fs_src).group(1))
    check("runtime 注册的 rank 小于 user-dsh 根（小者胜 ⇒ 组合包这份赢）",
          runtime_rank < user_rank,
          f"runtime={runtime_rank} vs user-dsh={user_rank}")

    # 实证：用**真实的** SkillRegistry 跑一遍合并，看谁最终胜出。
    if not node_available():
        check("node 可用（跑实证合并）", False, warn_only=True)
    else:
        (ROOT / ".cache-test").mkdir(exist_ok=True)
        harness = f'''
import {{ Context }} from {json.dumps((Path(DSH_CHECKOUT) / "vendor" / "cordis" / "src" / "index.ts").as_uri())};
import {{ SkillRegistry }} from {json.dumps(dsh_skill_src.as_uri())};

const root = new Context();
await root.plugin(SkillRegistry);
const skills = root.skills;

// 组合包注册的那一份（runtime，rank 250）
skills.register({{
  name: "gamemod-mod",
  description: "BUNDLED",
  source: "bundled",
  provider: "gamemod-mcp",
  content: "bundle-body",
}});

// 模拟 ~/.dsh/skills/ 里的旧副本（user-dsh，rank 400）
skills.registerProvider(() => ({{
  name: "skill-filesystem",
  list: async () => [{{
    name: "gamemod-mod",
    description: "STALE",
    invocation: {{ modelInvocable: true, userInvocable: true }},
    source: "user-dsh",
    provider: "skill-filesystem",
    rank: {user_rank},
    locator: "stale",
  }}],
  get: async () => undefined,
}}));

const list = await skills.list({{}});
const won = list.find(s => s.name === "gamemod-mod");
console.log(JSON.stringify({{ source: won.source, description: won.description }}));
'''
        r = run_node(harness, tsx=True, cwd=DSH_CHECKOUT)
        try:
            out = json.loads(r.stdout.strip().splitlines()[-1])
            check("实证：组合包注册的 skill 胜出，旧副本被盖住",
                  out.get("source") == "bundled",
                  f"实际胜出 source={out.get('source')!r}"
                  "（若为 user-dsh，说明旧副本会盖掉随包内容，机制失效）")
        except Exception:
            check("实证：组合包注册的 skill 胜出，旧副本被盖住", False,
                  (r.stdout or r.stderr or "")[-400:])

# ================================================================
section("8. 非空转反证：把 patch 改坏，断言必须报红")

def patch_is_bad(text: str) -> list[str]:
    """返回这条 patch 文本触发的**问题清单**（空 = 干净）。

    判据与第 3 节一致，这里抽成纯函数以便对真实文件做反证。
    """
    body = "\n".join(
        line for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    problems = []
    if re.search(r"[A-Za-z]:[\\/]", body):
        problems.append("含绝对路径")
    if not (re.search(r"inject:\s*\[\s*gamemodToolchain\s*\]", text)
            or re.search(r"inject:\s*\n\s*-\s*gamemodToolchain", text)):
        problems.append("缺少 inject 顺序保证")
    if "!!js ctx.gamemodToolchain.python" not in text:
        problems.append("解释器未取自服务")
    return problems


check("判据对当前真实 patch 判为干净", patch_is_bad(patch_text) == [],
      f"problems={patch_is_bad(patch_text)}")

# 反证 1：塞回绝对路径
counter1 = patch_text.replace(
    "command: !!js ctx.gamemodToolchain.python",
    'command: "C:/Python311/python.exe"',
)
check("反证：写死解释器路径会被判为坏", patch_is_bad(counter1) != [],
      f"problems={patch_is_bad(counter1)}")

# 反证 2：去掉 inject
counter2 = re.sub(r"\n\s*inject:\s*\[gamemodToolchain\]", "", patch_text)
check("反证：去掉 inject 会被判为坏", patch_is_bad(counter2) != [],
      f"problems={patch_is_bad(counter2)}")

# 反证 3：判据不是恒真/恒假
check("反证：判据不是恒假（真实 patch 干净、坏 patch 报红）",
      patch_is_bad(patch_text) == [] and patch_is_bad(counter1) != [])

# ================================================================
section("9. 反证：真的把坏 patch 放到磁盘再跑一次静态检查")

# 上面第 6 节验的是**判据函数**；这一节验「脚本真的会用它去读文件」——
# 否则可能出现「判据对、但脚本读错了文件」这种假绿。
#
# ⚠️ 两条必须遵守的纪律（第一版都违反了，代价很实在）：
#
#  1. **不许改写真产物**。第一版直接覆盖 `cordis.patch.yml` 再跑自己，
#     而那一轮被中断，于是**把产物本身改坏了**（留下一个写死路径的 patch）。
#     改为把坏内容写到 `.cache-test/` 下的临时副本，用环境变量指过去。
#  2. **必须带重入标记**。子进程跑到本节又会起孙进程 —— 第一版就是这样
#     把整轮挂死（不是报错，是静默卡住，更难查）。
if os.environ.get("GAMEMOD_DSH_INNER") == "1":
    print("  （内层运行：跳过本节，避免无限递归）")
else:
    cache = ROOT / ".cache-test"
    cache.mkdir(parents=True, exist_ok=True)
    fake = cache / "counter-patch.yml"
    fake.write_text(counter1, encoding="utf-8", newline="")
    try:
        env = dict(os.environ)
        env["GAMEMOD_DSH_INNER"] = "1"
        env["GAMEMOD_DSH_PATCH"] = str(fake)
        r = subprocess.run(
            [sys.executable, os.path.abspath(__file__)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(ROOT), env=env, timeout=600,
        )
        check("把坏 patch 放到磁盘后，脚本退出码非零",
              r.returncode != 0,
              "说明脚本确实读了磁盘上的文件，而不是只对内存里的字符串求值")
        check("输出里点出了绝对路径问题",
              "绝对路径" in (r.stdout or ""))
        # 非空转：确认内层**真的检查了那份坏副本**，而不是因为找不到文件
        # 而退出非零（那样也算"非零"，但证明不了判据有效）。
        check("内层确实读到坏副本（而非因文件缺失而失败）",
              "反证：写死解释器路径会被判为坏" in (r.stdout or ""))
    finally:
        try:
            fake.unlink()
        except OSError:
            pass

# ================================================================
print()
print("=" * 78)
print(f"通过 {PASS} 项，失败 {FAIL} 项" + (f"，警告 {WARN} 项" if WARN else ""))
print("=" * 78)
sys.exit(1 if FAIL else 0)
