#!/usr/bin/env python3
"""GameMod MCP 一键安装（**非 DSH 场景**用；DSH 里不需要跑这个）。

装到哪儿取决于你怎么用它
============================================================
  · **在 DSH 里用** —— 不用跑本脚本。本项目是一个 DSH 组合包，
    在 DSH 里安装这个目录即可：组合包会自己在
    `$DSH_HOME/gamemod-mcp/venv` 下建一个专用 venv 并装好依赖。
  · **接其它 MCP 客户端**（Claude Code / Codex / Cursor / …）——
    跑本脚本，它把依赖装进**当前解释器**，然后按 `mcp-config/` 填配置。

为什么主推 Python 而不是 PowerShell 脚本
============================================================
Windows 自带的 **PowerShell 5.1**（`powershell.exe`）读**无 BOM 的 UTF-8**
脚本时会按系统 ANSI（中文机器上是 GBK）解码 —— 脚本里的中文会变乱码，
进而**语法报错**。实测：

    powershell -File install.ps1
    → At install.ps1:2 char:14
      The string is missing the terminator: ".

而 `pwsh`（PowerShell 7）**不一定装了**。所以：

  · 本项目的编码规范要求「UTF-8 无 BOM」（`check_encoding.ps1` 强制），
    与 PowerShell 5.1 的需求**直接冲突**；
  · 对方装了 Python 才能跑 MCP —— **Python 一定在**。

结论：主推本脚本（纯 Python，无编码问题），`install.ps1` 降为备选
（给「已有 pwsh 7」的用户）。

用法
============================================================
    python install.py              # 装必需依赖 + 冒烟测试
    python install.py --vision     # 连可选弱视觉（截图/OCR）一起装
    python install.py --check      # 只做环境自检，不装东西
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
WHEELS_REQ = ROOT / "wheels" / "required"
WHEELS_OPT = ROOT / "wheels" / "optional"

#: 必需依赖。mcp **必须 <2**：本代码用 1.x 的 `mcp.server.fastmcp.FastMCP`，
#: 2.x 已改名 MCPServer。只写 `>=1.0.0` 会让干净环境装到 2.x 然后起不来
#: （打包分享包时实测到）。
REQUIRED = ["mcp>=1.0.0,<2", "frida>=16.0.0"]
OPTIONAL = ["dxcam", "mss", "rapidocr-onnxruntime", "onnxruntime", "numpy",
            "opencv-python", "pillow", "pywin32"]

OK = "[OK]"
BAD = "[FAIL]"
WARN = "[!]"


def say(msg: str = "") -> None:
    print(msg, flush=True)


def pip(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "pip", *args],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace")


def parse_requirements() -> list[str]:
    """从 requirements.txt 读依赖（按 UTF-8，绕开 pip -r 的编码坑）。"""
    req = ROOT / "requirements.txt"
    if not req.is_file():
        return list(REQUIRED)
    specs = []
    for line in req.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            specs.append(line)
    return specs or list(REQUIRED)


def install_specs(specs: list[str], wheels_dir: Path, label: str) -> bool:
    """先试离线（包内 wheel），失败再走网络。返回是否成功。"""
    if wheels_dir.is_dir() and any(wheels_dir.glob("*.whl")):
        n = len(list(wheels_dir.glob("*.whl")))
        say(f"  尝试离线安装（包内 {n} 个 wheel，无需外网）…")
        r = pip("install", "--no-index", "--find-links", str(wheels_dir), *specs)
        if r.returncode == 0:
            say(f"  {OK} 离线安装成功（{label}）")
            return True
        say(f"  {WARN} 离线装不上，回退到网络…")
    say(f"  从网络安装 {label}：{', '.join(specs)}")
    r = pip("install", *specs)
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "").strip().splitlines()
        say(f"  {BAD} 安装失败：{tail[-1] if tail else '（无输出）'}")
        say("       公司网络下可试镜像源，例如：")
        say("       python -m pip install -i "
            "https://pypi.tuna.tsinghua.edu.cn/simple " + " ".join(specs))
        return False
    say(f"  {OK} 安装成功（{label}）")
    return True


def smoke_test() -> tuple[bool, int]:
    """用包内 src 起一次服务器，数工具数。这是**最强**的一条验证。"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    env["PYTHONUTF8"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    code = ("import asyncio,sys;"
            f"sys.path.insert(0, r'{SRC}');"
            "from gamemod_mcp import server;"
            "print('TOOLCOUNT=',len(asyncio.run(server.mcp.list_tools())))")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, encoding="utf-8", errors="replace",
                       env=env, timeout=180)
    out = (r.stdout or "") + (r.stderr or "")
    for ln in out.splitlines():
        if "TOOLCOUNT=" in ln:
            try:
                return True, int(ln.split("TOOLCOUNT=")[1].strip())
            except Exception:
                pass
    say("  冒烟测试输出：")
    for ln in out.strip().splitlines()[-12:]:
        say("    " + ln)
    return False, 0


def main() -> int:
    ap = argparse.ArgumentParser(description="GameMod MCP 一键安装")
    ap.add_argument("--vision", action="store_true",
                    help="连可选弱视觉（截图 / OCR）一起装")
    ap.add_argument("--check", action="store_true",
                    help="只做环境自检，不安装任何东西")
    args = ap.parse_args()

    say()
    say("=" * 46)
    say(" GameMod MCP 安装")
    say("=" * 46)
    say()

    # ---------------------------------------------------------- 1/4 Python
    say(f"[1/4] 检查 Python…")
    v = sys.version_info
    if v < (3, 10):
        say(f"  {BAD} Python 版本过低：{v.major}.{v.minor}.{v.micro}"
            f"（需要 3.10+）")
        say("       请从 https://www.python.org/downloads/ 安装，"
            "并勾选 Add python.exe to PATH")
        return 1
    say(f"  {OK} Python {v.major}.{v.minor}.{v.micro}")
    say(f"       解释器：{sys.executable}")
    if "WindowsApps" in sys.executable:
        say(f"  {WARN} 这看起来是 Microsoft Store 的占位程序，"
            f"装依赖可能失败。建议装 python.org 的正式版。")

    if args.check:
        say()
        say("[--check] 只做自检，不安装。")
        ok, n = smoke_test()
        if ok:
            say(f"  {OK} 服务器可启动，注册了 {n} 个工具")
            return 0
        say(f"  {BAD} 服务器起不来 —— 依赖可能没装全")
        return 1

    # ---------------------------------------------------------- 2/4 必需依赖
    say()
    say("[2/4] 安装必需依赖（mcp + frida）…")
    specs = parse_requirements()
    if not install_specs(specs, WHEELS_REQ, "必需依赖"):
        return 1

    # ---------------------------------------------------------- 3/4 可选视觉
    say()
    if args.vision:
        say("[3/4] 安装可选弱视觉（截图 + OCR）…")
        if not install_specs(OPTIONAL, WHEELS_OPT, "弱视觉"):
            say(f"  {WARN} 弱视觉没装上 —— **不影响**其它工具，可稍后重试")
    else:
        say("[3/4] 跳过可选弱视觉（截图 / OCR）。")
        say("      要装的话重跑：python install.py --vision")
        say("      （不装也能用元数据查询、建索引、Hook、读内存等大部分功能）")

    # ---------------------------------------------------------- 4/4 冒烟
    say()
    say("[4/4] 冒烟测试：确认工具能注册…")
    ok, n = smoke_test()
    if not ok:
        say(f"  {BAD} 冒烟测试失败 —— 依赖可能没装对")
        return 1
    if n < 38:
        say(f"  {WARN} 只注册了 {n} 个工具（预期 >= 38）—— 包可能不完整")
    else:
        say(f"  {OK} 服务器正常，注册了 {n} 个工具")

    # ---------------------------------------------------------- 配置提示
    say()
    say("=" * 46)
    say(" 安装完成。MCP 客户端配置（**路径按实际位置填**）：")
    say()
    say(f"  命令 : {sys.executable}")
    say(f"  参数 : {SRC / 'gamemod_mcp' / 'server.py'}")
    say(f"  环境 : PYTHONPATH={SRC}")
    say("         PYTHONUTF8=1")
    say()
    say(f"  现成样例见 mcp-config{os.sep}（README.md 有各客户端说明）")
    say()
    say("  提示：注入游戏进程**不需要管理员权限**（实测）。")
    say("        只要 Agent 与游戏以相同权限运行即可；")
    say("        只有游戏本身以管理员运行时才需要提权。")
    say()
    say("=" * 46)
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
