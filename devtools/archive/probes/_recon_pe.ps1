# 临时侦察脚本（验证 PE → CLI 头 → 元数据根 → #~ 表头 的字节布局）
param([string]$Path)
$ErrorActionPreference = 'Stop'
$d = [System.IO.File]::ReadAllBytes($Path)
Write-Output "file=$Path size=$($d.Length)"

function U16([int]$o) { [BitConverter]::ToUInt16($d, $o) }
function U32([int]$o) { [BitConverter]::ToUInt32($d, $o) }
function I32([int]$o) { [BitConverter]::ToInt32($d, $o) }
function U64([int]$o) { [BitConverter]::ToUInt64($d, $o) }

$e_lfanew = I32 0x3C
Write-Output ("e_lfanew={0:X}" -f $e_lfanew)
$coff = $e_lfanew + 4
$machine = U16 $coff
$numSec = U16 ($coff + 2)
$optSize = U16 ($coff + 16)
$opt = $coff + 20
$magic = U16 $opt
$pe32plus = ($magic -eq 0x20B)
Write-Output "machine=$('{0:X}' -f $machine) sections=$numSec optSize=$optSize magic=$('{0:X}' -f $magic) pe32plus=$pe32plus"

$ddOff = $opt + $(if ($pe32plus) { 112 } else { 96 })
$numDirs = U32 ($opt + $(if ($pe32plus) { 108 } else { 92 }))
Write-Output "numDirs=$numDirs"
$clrRva = U32 ($ddOff + 14 * 8)
$clrSize = U32 ($ddOff + 14 * 8 + 4)
Write-Output ("CLR dir rva={0:X} size={1:X}" -f $clrRva, $clrSize)

# 节表
$secStart = $opt + $optSize
$secs = @()
for ($i = 0; $i -lt $numSec; $i++) {
  $b = $secStart + $i * 40
  $name = [System.Text.Encoding]::ASCII.GetString($d, $b, 8).TrimEnd([char]0)
  $vsize = U32 ($b + 8); $vaddr = U32 ($b + 12); $rsize = U32 ($b + 16); $rptr = U32 ($b + 20)
  $secs += [pscustomobject]@{ name = $name; vaddr = $vaddr; vsize = $vsize; rsize = $rsize; rptr = $rptr }
}
$secs | ForEach-Object { Write-Output ("  sec {0,-8} rva={1:X8} vsize={2:X} raw={3:X8} rawsize={4:X}" -f $_.name, $_.vaddr, $_.vsize, $_.rptr, $_.rsize) }

function RvaToOff([int]$rva) {
  foreach ($s in $secs) {
    $sz = [Math]::Max($s.vsize, $s.rsize)
    if ($rva -ge $s.vaddr -and $rva -lt ($s.vaddr + $sz)) {
      $delta = $rva - $s.vaddr
      if ($delta -lt $s.rsize) { return $s.rptr + $delta }
      return -1
    }
  }
  return -1
}

$cliOff = RvaToOff $clrRva
Write-Output ("CLI header file offset={0:X}" -f $cliOff)
Write-Output ("  cb={0:X} major={1} minor={2}" -f (U32 $cliOff), (U16 ($cliOff+4)), (U16 ($cliOff+6)))
$mdRva = U32 ($cliOff + 8)
$mdSize = U32 ($cliOff + 12)
Write-Output ("  MetaData rva={0:X} size={1:X}" -f $mdRva, $mdSize)
Write-Output ("  flags={0:X} entrypoint={1:X}" -f (U32 ($cliOff+16)), (U32 ($cliOff+20)))
Write-Output ("  resources={0:X}/{1:X} strongname={2:X}/{3:X}" -f (U32($cliOff+24)), (U32($cliOff+28)), (U32($cliOff+32)), (U32($cliOff+36)))

$mdOff = RvaToOff $mdRva
Write-Output ("MetaData file offset={0:X}" -f $mdOff)
Write-Output ("  signature={0:X8} (expect 424A5342)" -f (U32 $mdOff))
Write-Output ("  major={0} minor={1} reserved={2:X} versionLen={3}" -f (U16 ($mdOff+4)), (U16 ($mdOff+6)), (U32 ($mdOff+8)), (U32 ($mdOff+12)))
$verLen = U32 ($mdOff + 12)
$verEnd = $mdOff + 16
while ($d[$verEnd] -ne 0) { $verEnd++ }
Write-Output ("  version='{0}' (declared len {1}, actual {2})" -f ([System.Text.Encoding]::UTF8.GetString($d, $mdOff+16, $verEnd-($mdOff+16))), $verLen, ($verEnd-($mdOff+16)))
$afterVer = $mdOff + 16 + $verLen
$alignedVer = [int](([Math]::Floor(($verLen + 3) / 4)) * 4)
Write-Output ("  afterVer(unaligned)={0:X}  afterVer(aligned+3&~3)={1:X}  next4={2:X}" -f $afterVer, ($mdOff+16+$alignedVer), ($mdOff+16+$alignedVer+4))

$flags = U16 ($mdOff + 16 + $alignedVer)
$streams = U16 ($mdOff + 16 + $alignedVer + 2)
Write-Output ("  flags={0:X} streamCount={1} at offset {2:X}" -f $flags, $streams, ($mdOff+16+$alignedVer))

$p = $mdOff + 16 + $alignedVer + 4
for ($i = 0; $i -lt $streams; $i++) {
  $soff = U32 $p; $ssize = U32 ($p+4)
  $nStart = $p + 8
  $nEnd = $nStart
  while ($d[$nEnd] -ne 0) { $nEnd++ }
  $nm = [System.Text.Encoding]::ASCII.GetString($d, $nStart, $nEnd-$nStart)
  Write-Output ("  stream[{0}] '{1}' off={2:X} size={3:X}" -f $i, $nm, $soff, $ssize)
  $p = $nStart + [int]([Math]::Ceiling(($nEnd-$nStart+1)/4.0))*4

  if ($nm -eq '#~' -or $nm -eq '#-') {
    $t = $mdOff + $soff
    Write-Output ("    reserved={0:X} major={1} minor={2} heapSizes={3:X} reserved2={4:X}" -f (U32 $t), $d[$t+4], $d[$t+5], $d[$t+6], $d[$t+7])
    $valid = U64 ($t+8)
    $sorted = U64 ($t+16)
    Write-Output ("    valid={0:X16} sorted={1:X16}" -f $valid, $sorted)
    $rp = $t + 24
    for ($k = 0; $k -lt 64; $k++) {
      if (($valid -shr $k) -band 1) {
        Write-Output ("      rows[{0,2}] table=0x{0:X2} = {1}" -f $k, (U32 $rp))
        $rp += 4
      }
    }
    Write-Output ("    rowsEnd offset (relative to table start) = {0:X}" -f ($rp - $t))
  }
}
