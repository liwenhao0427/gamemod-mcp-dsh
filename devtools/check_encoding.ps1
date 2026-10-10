# 校验交付目录里所有文本文件的编码：必须是 UTF-8 且不含 BOM
# 依据 ~/.dsh/AGENTS.md 的编码要求

$ErrorActionPreference = 'Stop'
# 脚本放在 devtools/ 下，要校验的是**整个交付目录**，所以上跳一级。
# （用 $PSScriptRoot 会只扫 devtools，那正是这个脚本第一版写错的地方。）
$root = Split-Path -Parent $PSScriptRoot

# 需要检查的文本文件类型
$patterns = @('*.py', '*.md', '*.js', '*.json', '*.toml', '*.txt',
              '*.ps1', '*.sh', '*.cs', '*.csproj')

$files = foreach ($p in $patterns) {
    Get-ChildItem -Path $root -Recurse -File -Filter $p -ErrorAction SilentlyContinue
}
$files = $files | Where-Object { $_.FullName -notmatch '__pycache__' } |
    Sort-Object FullName -Unique

$badBom = @()
$badDecode = @()

foreach ($f in $files) {
    $bytes = [System.IO.File]::ReadAllBytes($f.FullName)

    # UTF-8 BOM = EF BB BF
    if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and
        $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
        $badBom += $f.FullName
        continue
    }

    # 用严格 UTF-8 解码，失败即为非 UTF-8
    try {
        $strict = [System.Text.UTF8Encoding]::new($false, $true)
        $null = $strict.GetString($bytes)
    }
    catch {
        $badDecode += $f.FullName
    }
}

Write-Output "检查文件数: $($files.Count)"
Write-Output ''

if ($badBom.Count -eq 0) {
    Write-Output '带 BOM 的文件: 0（符合要求）'
}
else {
    Write-Output "带 BOM 的文件: $($badBom.Count)"
    $badBom | ForEach-Object { Write-Output "  $_" }
}

if ($badDecode.Count -eq 0) {
    Write-Output '非 UTF-8 的文件: 0（符合要求）'
}
else {
    Write-Output "非 UTF-8 的文件: $($badDecode.Count)"
    $badDecode | ForEach-Object { Write-Output "  $_" }
}

Write-Output ''
if ($badBom.Count -eq 0 -and $badDecode.Count -eq 0) {
    Write-Output '编码校验通过'
    exit 0
}
Write-Output '编码校验未通过'
exit 1
