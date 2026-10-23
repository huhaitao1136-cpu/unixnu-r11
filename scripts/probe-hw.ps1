<#
.SYNOPSIS
  从 OPPO/sdm660 stock 内核源码树里抓出移植需要的硬件参数（M0 取证的第 1 步）。

.DESCRIPTION
  只读扫描 arch/arm64/boot/dts/ 与 arch/arm64/configs/，输出一张可直接填进
  board.toml / r11.dts 的表：panel 初始化序列、触摸 IC、eMMC/SD 节点、USB、
  中断控制器、DDR 起始地址。

.EXAMPLE
  .\probe-hw.ps1 -KernelSrc E:\src\android_kernel_oppo_sdm660-9
  .\probe-hw.ps1 -KernelSrc E:\src\android_kernel_oppo_sdm660-9 -Json out.json
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$KernelSrc,
    [string]$OutJson
)

$ErrorActionPreference = 'Stop'
$dtsDir = Join-Path $KernelSrc 'arch\arm64\boot\dts\qcom'
$cfgDir = Join-Path $KernelSrc 'arch\arm64\configs'
if (-not (Test-Path $dtsDir)) { throw "找不到 $dtsDir —— KernelSrc 指向内核树根目录" }

function Find-First($pattern, $path = $dtsDir) {
    Get-ChildItem -Path $path -Recurse -Include *.dts,*.dtsi -ErrorAction SilentlyContinue |
        Select-String -Pattern $pattern -CaseSensitive:$false |
        Select-Object -First 3
}

$report = [ordered]@{}

# 1) 板级 dts（名字里带 r11 / oppo / 1707）
$report['board_dts'] = (Get-ChildItem $dtsDir -Filter *.dts |
    Where-Object { $_.Name -match 'r11|oppo|1707' } | Select-Object -ExpandProperty Name)

# 2) 屏幕
$report['panel_name'] = (Find-First 'qcom,mdss-dsi-panel-name')
$report['panel_res']  = (Find-First 'qcom,mdss-dsi-panel-width|qcom,mdss-dsi-panel-height')
$report['panel_on']   = (Find-First 'qcom,mdss-dsi-on-command|qcom,mdss-dsi-init-command')

# 3) 触摸
$report['touch'] = (Find-First 'focaltech|goodix|synaptics|ft8716|ft8|gt9|qcom,ts-')

# 4) 存储
$report['emmc'] = (Find-First 'sdhc_1|sdhci@c0c4000')
$report['sd']   = (Find-First 'sdhc_2|sdhci@c084000')

# 5) USB / PHY
$report['usb'] = (Find-First 'dwc3@|qusb2|qcom,dwc3')

# 6) 中断控制器（GIC v2 还是 v3 —— 决定 defconfig 的 CONFIG_GIC/GICV3）
$report['gic'] = (Find-First 'intc:\s*interrupt-controller|compatible = "arm,gic')

# 7) DDR 基址
$report['ddr'] = (Find-First 'memory@[0-9a-f]+|device_type = "memory"')

# 8) 电源
$report['pmic'] = (Find-First 'pm660|spmi@')

# 9) stock defconfig 里的关键开关
if (Test-Path $cfgDir) {
    $report['defconfigs'] = (Get-ChildItem $cfgDir -Filter '*sdm660*' |
        Select-Object -ExpandProperty Name)
    $report['cfg_touch_sd'] = (Get-ChildItem $cfgDir -Filter '*sdm660*' |
        Select-String -Pattern 'TOUCHSCREEN|SDHCI|QCOM_GCC|PM660' |
        Select-Object -First 20)
}

# ── 输出 ────────────────────────────────────────────────────────────────
"=== OPPO R11 / SDM660 取证表 ==="
foreach ($k in $report.Keys) {
    "`n--- $k ---"
    $v = $report[$k]
    if ($null -eq $v -or $v.Count -eq 0) { '  (未找到 —— 换 pattern 或确认源码版本)'; continue }
    $v | ForEach-Object {
        if ($_ -is [System.Management.Automation.MatchInfo]) {
            "  {0}:{1}: {2}" -f $_.Filename, $_.LineNumber, $_.Line.Trim()
        } elseif ($_ -is [string]) { "  $_" }
        else { "  $($_.ToString())" }
    }
}

if ($OutJson) {
    $report | ConvertTo-Json -Depth 4 | Set-Content -Encoding utf8 $OutJson
    "`n已写出 $OutJson"
}
