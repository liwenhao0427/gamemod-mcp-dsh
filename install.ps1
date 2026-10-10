# GameMod MCP 一键安装脚本（Windows PowerShell）
#
# 用法：
#     pwsh -ExecutionPolicy Bypass -File .\install.ps1
#
# 做三件事：
#     1. 检查 Python 版本
#     2. 装依赖（mcp + frida）
#     3. 冒烟测试：起一次服务器，确认工具能正常注册
#
# 不会修改系统 Python 环境以外的东西，也不会写注册表。

$ErrorActionPreference = 'Stop'

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host " GameMod MCP 安装" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

$Root = $PSScriptRoot
$Src  = Join-Path $Root 'src'

# ---------------------------------------------------------------- 1. Python
Write-Host "[1/4] 检查 Python…" -ForegroundColor Yellow

$py = $null
foreach ($cand in @('python', 'python3', 'py')) {
    $cmd = Get-Command $cand -ErrorAction SilentlyContinue
    if ($cmd) { $py = $cmd.Source; break }
}

if (-not $py) {
    Write-Host "  ✗ 没找到 Python。请先安装 Python 3.10+ 并加入 PATH。" -ForegroundColor Red
    Write-Host "    https://www.python.org/downloads/" -ForegroundColor Red
    exit 1
}

$ver = & $py -c "import sys; print('%d.%d.%d' % sys.version_info[:3])"
$ok  = & $py -c "import sys; print(1 if sys.version_info >= (3,10) else 0)"
if ($ok -ne '1') {
    Write-Host "  ✗ Python 版本过低：$ver（需要 3.10+）" -ForegroundColor Red
    exit 1
}
Write-Host "  ✓ Python $ver" -ForegroundColor Green
Write-Host "    路径: $py"

# 提示 WindowsApps 那个假 python
if ($py -like '*WindowsApps*') {
    Write-Host "  ⚠ 这看起来是 Microsoft Store 的占位程序，可能会失败。" -ForegroundColor Yellow
    Write-Host "    建议装 python.org 的正式版，或显式用另一个解释器。" -ForegroundColor Yellow
}

# ---------------------------------------------------------------- 2. 依赖
Write-Host ""
Write-Host "[2/4] 安装依赖（mcp + frida）…" -ForegroundColor Yellow

# 注意：requirements.txt 是 UTF-8（含中文注释），而 pip 读 -r 文件时只在
# 有 BOM 的情况下才认 UTF-8，否则会用系统区域编码（中文 Windows 上是 GBK），
# 于是直接 `pip install -r` 会 UnicodeDecodeError。
# 这里改用 Python 显式按 UTF-8 读出包名，再作为参数交给 pip —— 既绕开
# 编码问题，又不用把 requirements.txt 降级成纯 ASCII 或加 BOM。
$req = Join-Path $Root 'requirements.txt'
$specs = @()
if (Test-Path $req) {
    $extract = @'
import sys
specs = []
with open(sys.argv[1], "r", encoding="utf-8") as fh:
    for line in fh:
        line = line.split("#", 1)[0].strip()
        if line:
            specs.append(line)
sys.stdout.write("\n".join(specs))
'@
    $tmp = Join-Path $env:TEMP ("gamemod_req_{0}.py" -f (Get-Random))
    Set-Content -Path $tmp -Value $extract -Encoding UTF8
    try {
        $specs = @(& $py $tmp $req) | Where-Object { $_ -and $_.Trim() }
    } finally {
        Remove-Item $tmp -Force -ErrorAction SilentlyContinue
    }
}

if ($specs.Count -gt 0) {
    Write-Host "  待安装: $($specs -join ', ')" -ForegroundColor DarkGray
    & $py -m pip install @specs
} else {
    # requirements.txt 缺失或为空时的兜底。
    # ⚠️ mcp 必须 <2（理由见 requirements.txt 里的说明）—— 只写 >=1.0.0
    # 会让全新环境装到 mcp 2.x，然后 `mcp.server.fastmcp` 导入失败。
    & $py -m pip install 'mcp>=1.0.0,<2' 'frida>=16.0.0'
}
if ($LASTEXITCODE -ne 0) {
    Write-Host "  ✗ 依赖安装失败。若在公司网络下，可能需要配置 pip 镜像源。" -ForegroundColor Red
    exit 1
}
Write-Host "  ✓ 依赖就绪" -ForegroundColor Green

# ---------------------------------------------------------------- 3. 冒烟测试
Write-Host ""
Write-Host "[3/4] 冒烟测试：确认工具能正常注册…" -ForegroundColor Yellow

$env:PYTHONPATH = $Src
$smoke = @'
import sys, asyncio
sys.path.insert(0, r"__SRC__")
from gamemod_mcp import server
async def main():
    tools = await server.mcp.list_tools()
    print(f"TOOLCOUNT={len(tools)}")
asyncio.run(main())
'@.Replace('__SRC__', $Src)

$tmp = Join-Path $env:TEMP ("gamemod_smoke_{0}.py" -f (Get-Random))
Set-Content -Path $tmp -Value $smoke -Encoding UTF8
try {
    $out = & $py $tmp 2>&1
    $line = $out | Where-Object { $_ -match 'TOOLCOUNT=' } | Select-Object -First 1
    if ($line) {
        $n = ($line -split '=')[1]
        Write-Host "  ✓ 服务器正常，注册了 $n 个工具" -ForegroundColor Green
    } else {
        Write-Host "  ✗ 冒烟测试失败，输出如下：" -ForegroundColor Red
        $out | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
        exit 1
    }
} finally {
    Remove-Item $tmp -Force -ErrorAction SilentlyContinue
}

# ---------------------------------------------------------------- 4. 配置提示
Write-Host ""
Write-Host "[4/4] 完成。接下来配置你的 Agent：" -ForegroundColor Yellow
Write-Host ""
Write-Host "  在 MCP 客户端配置里填：" -ForegroundColor White
Write-Host "    命令 : $py" -ForegroundColor Gray
Write-Host "    参数 : $Src\gamemod_mcp\server.py" -ForegroundColor Gray
Write-Host "    环境 : PYTHONPATH=$Src" -ForegroundColor Gray
Write-Host "           PYTHONUTF8=1" -ForegroundColor Gray
Write-Host ""
Write-Host "  各客户端的现成配置在 mcp-config\ 目录，详见 mcp-config\README.md" -ForegroundColor Gray
Write-Host ""

Write-Host "  提示：注入游戏进程**不需要管理员权限**（实测）。" -ForegroundColor Gray
Write-Host "        只要 Agent 与游戏以相同权限运行即可；" -ForegroundColor Gray
Write-Host "        只有游戏本身以管理员运行时才需要提权。" -ForegroundColor Gray
Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host " 安装完成" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Cyan
