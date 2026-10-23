# make-r11pkg.ps1 —— 从 Mu-Silicium 的 lavenderPkg（同为 SDM660）生成 R11 平台包
#
# 为什么从 lavender 派生：
#   - 同为 SDM660（Silicon/Qualcomm/SDM660Pkg 直接复用）
#   - lavenderPkg 的 DeviceMemoryMapLib 与 R11 实测内存布局一致
#     （Display Reserved 0x9D400000；R11 的 splash_region 实测也是 0x9D400000 + 0x2300000）
#   - 显示走 cont-splash：SimpleFbDxe 只认 "Display Reserved" 区，不自己点屏
#
# 用法（Windows PowerShell 5.1 需要 -ExecutionPolicy Bypass）：
#   powershell -NoProfile -ExecutionPolicy Bypass -File make-r11pkg.ps1 `
#       -Mu E:\DeepSeekWorkspace\src\Mu-Silicium -Out E:\DeepSeekWorkspace\uefi-r11
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Mu,
    [Parameter(Mandatory)][string]$Out,
    [string]$Guid = "9F2C1A10-4D3E-4F5A-9C11-2E7B6A1D8C31"
)

$ErrorActionPreference = 'Stop'
$src = Join-Path $Mu 'Platforms\Xiaomi\lavenderPkg'
$dst = Join-Path $Out 'Platforms\OPPO\r11Pkg'
if (-not (Test-Path $src)) { throw "找不到 $src —— -Mu 应指向 Mu-Silicium 仓库根目录" }
if (Test-Path $dst) { Remove-Item $dst -Recurse -Force }
New-Item -ItemType Directory -Force (Split-Path $dst) | Out-Null
Copy-Item $src $dst -Recurse

foreach ($ext in 'dsc', 'fdf', 'dec') {
    $old = Join-Path $dst "lavender.$ext"
    if (Test-Path $old) { Move-Item $old (Join-Path $dst "r11.$ext") -Force }
}

$textExt = @('.dsc', '.fdf', '.dec', '.inc', '.c', '.h', '.inf', '.py', '.conf', '.md')
$repl = New-Object 'System.Collections.Specialized.OrderedDictionary'
$repl.Add('Binaries/lavender', 'Binaries/r11')
$repl.Add('lavenderUEFI', 'r11UEFI')
$repl.Add('lavender_UEFI', 'r11_UEFI')
$repl.Add('lavenderPkg', 'r11Pkg')
$repl.Add('Platforms/Xiaomi', 'Platforms/OPPO')
$repl.Add('DAA08544-071F-4A77-BD10-2D2EC946EA2A', $Guid.ToUpper())
$repl.Add('PLATFORM_NAME                  = lavender', 'PLATFORM_NAME                  = r11')
$repl.Add('OUTPUT_DIRECTORY               = Build/lavenderPkg', 'OUTPUT_DIRECTORY               = Build/r11Pkg')
$repl.Add('FLASH_DEFINITION               = lavenderPkg/lavender.fdf', 'FLASH_DEFINITION               = r11Pkg/r11.fdf')
$repl.Add('PcdMipiFrameBufferHeight|2340', 'PcdMipiFrameBufferHeight|1920')
$repl.Add('PcdSmbiosSystemVendor|"Xiaomi"', 'PcdSmbiosSystemVendor|"OPPO"')
$repl.Add('PcdSmbiosSystemModel|"Redmi Note 7"', 'PcdSmbiosSystemModel|"OPPO R11"')
$repl.Add('PcdSmbiosSystemRetailModel|"lavender"', 'PcdSmbiosSystemRetailModel|"r11"')
$repl.Add('PcdSmbiosSystemRetailSku|"Redmi_Note 7_lavender"', 'PcdSmbiosSystemRetailSku|"OPPO_R11_r11"')
$repl.Add('PcdSmbiosBoardModel|"Redmi Note 7"', 'PcdSmbiosBoardModel|"OPPO R11"')
$repl.Add('PcdDeviceMaintainer|"index986"', 'PcdDeviceMaintainer|"unixnu-r11"')

$utf8 = New-Object System.Text.UTF8Encoding($false)
$changed = @()
$files = Get-ChildItem -Path $dst -Recurse -File
foreach ($f in $files) {
    if ($textExt -notcontains $f.Extension) { continue }
    $t = [System.IO.File]::ReadAllText($f.FullName)
    $orig = $t
    foreach ($k in $repl.Keys) { $t = $t.Replace($k, $repl[$k]) }
    $t = $t.Replace('lavender', 'r11').Replace('Lavender', 'R11').Replace('Redmi Note 7', 'OPPO R11').Replace('Redmi', 'OPPO').Replace('Xiaomi', 'OPPO')
    if ($t -ne $orig) {
        [System.IO.File]::WriteAllText($f.FullName, $t, $utf8)
        $changed += $f.FullName.Replace($Out + '\', '')
    }
}

$confSrc = Join-Path $Mu 'Resources\Configs\lavender.conf'
$confDst = Join-Path $Out 'Resources\Configs\r11.conf'
New-Item -ItemType Directory -Force (Split-Path $confDst) | Out-Null
if (Test-Path $confSrc) {
    $c = [System.IO.File]::ReadAllText($confSrc).Replace('Xiaomi', 'OPPO')
    [System.IO.File]::WriteAllText($confDst, $c, $utf8)
}

Write-Host "=== 生成结果 ==="
Get-ChildItem -Path $dst -Recurse -File | ForEach-Object {
    "  {0}  ({1} B)" -f $_.FullName.Replace($dst + '\', ''), $_.Length
}
Write-Host ("`n配置: {0}" -f $confDst)

$leftover = @()
foreach ($f in (Get-ChildItem -Path $dst -Recurse -File)) {
    if ($textExt -notcontains $f.Extension) { continue }
    $m = Select-String -Path $f.FullName -Pattern 'lavender|Redmi|Xiaomi' -ErrorAction SilentlyContinue
    if ($m) { $leftover += $m }
}
if ($leftover.Count -gt 0) {
    Write-Warning "仍有残留："
    $leftover | Select-Object -First 10 | ForEach-Object { "  {0}:{1} {2}" -f (Split-Path $_.Path -Leaf), $_.LineNumber, $_.Line.Trim() }
} else {
    Write-Host "`n自检通过：无 lavender / Redmi / Xiaomi 残留"
}

Write-Host "`n改动的文件："
$changed | ForEach-Object { "  $_" }
