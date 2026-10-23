# flash-via-adb.ps1 — use adb + root to write a partition directly (instead of fastboot flash)
#
# Precondition: device is rooted (su works). The write path was verified on this device
# (1 MiB pattern written to `cache`, read back with an identical sha256).
# Risk: adb+dd writes to the partition (not a RAM-only boot), so a failure leaves half an
# image. This script therefore always backs up first and verifies the read-back, and
# supports -Rollback. For stage-1 trials prefer `fastboot boot` (nothing is written).
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File flash-via-adb.ps1 -Partition boot -BackupOnly
#   powershell -ExecutionPolicy Bypass -File flash-via-adb.ps1 -Image out\r11\boot.img -Yes
#   powershell -ExecutionPolicy Bypass -File flash-via-adb.ps1 -Rollback R11-backup\firmware\boot.img -Yes
[CmdletBinding()]
param(
    [string]$Image,
    [string]$Partition = 'boot',
    [string]$BackupDir = 'R11-backup\firmware\backup',
    [switch]$BackupOnly,
    [string]$Rollback,
    [switch]$NoReboot,
    [switch]$Yes
)

# 默认备份目录跟着脚本走：scripts -> unixnu-r11 -> 工作区根 -> R11-backup
if (-not $PSBoundParameters.ContainsKey('BackupDir')) {
    $root = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
    $BackupDir = Join-Path $root 'R11-backup\firmware\backup'
}

# native tools (adb) write progress to stderr; keep this at Continue so they don't abort
$ErrorActionPreference = 'Continue'

function Invoke-AdbShell([string]$cmd) {
    $out = & adb shell "su -c `"$cmd`"" 2>&1
    return (($out | Where-Object { $_ -notmatch 'daemon (not running|started)' }) -join "`n").Trim()
}

function Get-RemoteHash([string]$blockDev, [int64]$bytes) {
    $cnt = [int][math]::Ceiling($bytes / 4096)
    return (Invoke-AdbShell "dd if=$blockDev bs=4096 count=$cnt 2>/dev/null | sha256sum | cut -d' ' -f1")
}

# --- 0) device + root check -------------------------------------------------
$devs = & adb devices 2>&1 | Select-String -Pattern "`tdevice$"
if (-not $devs) { throw "no adb device (adb devices shows no 'device')" }
$who = (& adb shell "su -c id" 2>&1) -join ''
if ($who -notmatch 'uid=0') { throw "su unavailable: $who" }
Write-Host "[ok] device online, root available"

# --- 1) backup (always, except for rollback) --------------------------------
if (-not $Rollback) {
    New-Item -ItemType Directory -Force $BackupDir | Out-Null
    $size = (Invoke-AdbShell "blockdev --getsize64 /dev/block/by-name/$Partition").Trim()
    if (-not $size) { throw "cannot query size of /dev/block/by-name/$Partition" }
    Write-Host "[..] backup /dev/block/by-name/$Partition ($size B)"
    Invoke-AdbShell "dd if=/dev/block/by-name/$Partition of=/sdcard/__bak.img bs=4096 2>/dev/null; sync" | Out-Null
    $dst = Join-Path $BackupDir "$Partition-$(Get-Date -Format yyyyMMdd-HHmmss).img"
    & adb pull /sdcard/__bak.img $dst | Out-Null
    Invoke-AdbShell "rm -f /sdcard/__bak.img" | Out-Null
    $h = (Get-FileHash $dst -Algorithm SHA256).Hash.ToLower()
    Write-Host "[ok] backup: $dst  sha256=$h"
    if ($BackupOnly) { return }
}

# --- 2) rollback ------------------------------------------------------------
if ($Rollback) {
    if (-not (Test-Path $Rollback)) { throw "not found: $Rollback" }
    if (-not $Yes) { throw "writing a partition requires -Yes" }
    $len = [int64](Get-Item $Rollback).Length
    Write-Host "[!!] rollback $Rollback -> $Partition ($len B)"
    & adb push $Rollback /sdcard/__rb.img | Out-Null
    Invoke-AdbShell "dd if=/sdcard/__rb.img of=/dev/block/by-name/$Partition bs=4096 2>/dev/null; sync" | Out-Null
    $rh = Get-RemoteHash "/dev/block/by-name/$Partition" $len
    $lh = (Get-FileHash $Rollback -Algorithm SHA256).Hash.ToLower()
    Write-Host ("readback: {0}`nlocal   : {1}" -f $rh, $lh)
    Invoke-AdbShell "rm -f /sdcard/__rb.img" | Out-Null
    if ($rh -ne $lh) { throw "readback mismatch after rollback" }
    Write-Host "[ok] rollback verified"
    if (-not $NoReboot) { & adb reboot }
    return
}

# --- 3) flash ---------------------------------------------------------------
if (-not $Image) { throw "need -Image (or -BackupOnly / -Rollback)" }
if (-not (Test-Path $Image)) { throw "not found: $Image" }
$len = [int64](Get-Item $Image).Length
$psize = [int64](Invoke-AdbShell "blockdev --getsize64 /dev/block/by-name/$Partition").Trim()
if ($len -gt $psize) { throw "image $len B > partition $psize B" }
Write-Host ("[..] writing {0} ({1:N0} B) -> /dev/block/by-name/{2} ({3:N0} B)" -f $Image, $len, $Partition, $psize)
if (-not $Yes) { throw "writing a partition requires -Yes (prefer 'fastboot boot' for stage 1)" }

& adb push $Image /sdcard/__new.img | Out-Null
Invoke-AdbShell "dd if=/sdcard/__new.img of=/dev/block/by-name/$Partition bs=4096 2>/dev/null; sync" | Out-Null
$rh = Get-RemoteHash "/dev/block/by-name/$Partition" $len
$lh = (Get-FileHash $Image -Algorithm SHA256).Hash.ToLower()
Invoke-AdbShell "rm -f /sdcard/__new.img" | Out-Null
Write-Host ("readback: {0}`nlocal   : {1}" -f $rh, $lh)
if ($rh -ne $lh) { throw "readback mismatch -- roll back with -Rollback <backup file>" }
Write-Host "[ok] written and verified"
if (-not $NoReboot) { Write-Host "[..] rebooting"; & adb reboot }
