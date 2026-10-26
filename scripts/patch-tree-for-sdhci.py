#!/usr/bin/env python3
"""把 NativeSdhci 接入内核构建（改上游树里的两个脚本）。

用法（在 UniXNU 主仓库根目录执行）：
    python3 device/scripts/patch-tree-for-sdhci.py

改动内容：
  scripts/build-arm-driver.py
    1. 组件 choices 加入 'NativeSdhci'
    2. 输出目录映射 'NativeSdhci' -> build/native-sdhci
    3. 存储 ABI 头文件组加入 'NativeSdhci'（IOBlockStorageDevice.h 来自 build/storage-abi）
  scripts/build-xnu-4k-baseline.sh
    4. 新增 NATIVE_SDHCI 开关，打开时 -DXNU_NATIVE_SDHCI=1
    5. 打开时把 libNativeSdhci.a 用 -Wl,-force_load 链进内核

脚本可重复执行（已改过就跳过）。
"""
from pathlib import Path
import sys

ROOT = Path.cwd()
driver_script = ROOT / 'scripts' / 'build-arm-driver.py'
kernel_script = ROOT / 'scripts' / 'build-xnu-4k-baseline.sh'


def patch(path: Path, edits, label: str) -> None:
    if not path.is_file():
        sys.exit(f'找不到 {path}；请在 UniXNU 主仓库根目录执行')
    text = path.read_text()
    original = text
    for old, new in edits:
        if new in text:
            continue                     # 已经改过
        if old not in text:
            sys.exit(f'{label}: 找不到锚点，上游可能变了：{old[:70]!r}')
        text = text.replace(old, new, 1)
    if text != original:
        path.write_text(text)
        print(f'{label}: 已修补')
    else:
        print(f'{label}: 无需改动（已是最新）')


# ---- 1/2/3: build-arm-driver.py ----
patch(driver_script, [
    # 1. choices
    ("'NativeUfs', 'NativeHidInput',",
     "'NativeUfs', 'NativeSdhci', 'NativeHidInput',"),
    # 2. 输出目录
    ("'NativeUfs':'native-ufs', 'NativeHidInput':'native-hid-input',",
     "'NativeUfs':'native-ufs', 'NativeSdhci':'native-sdhci', 'NativeHidInput':'native-hid-input',"),
    # 3. 存储 ABI 头文件
    ("if component in ('NativeVirtioBlock', 'NativeRamBlock', 'NativeUfs'):",
     "if component in ('NativeVirtioBlock', 'NativeRamBlock', 'NativeUfs', 'NativeSdhci'):"),
], 'build-arm-driver.py')

# ---- 4/5: build-xnu-4k-baseline.sh ----
patch(kernel_script, [
    # 4. 开关 + personality CFLAGS
    ('''if [ "${XNU_NATIVE_UFS:-0}" = 1 ]; then
  PERSONALITY_CFLAGS="$PERSONALITY_CFLAGS -DXNU_NATIVE_UFS=1"
fi''',
     '''if [ "${XNU_NATIVE_UFS:-0}" = 1 ]; then
  PERSONALITY_CFLAGS="$PERSONALITY_CFLAGS -DXNU_NATIVE_UFS=1"
fi
NATIVE_SDHCI=${XNU_NATIVE_SDHCI:-0}
if [ "$NATIVE_SDHCI" != 0 ] && [ "$NATIVE_SDHCI" != 1 ]; then
  echo 'XNU_NATIVE_SDHCI must be 0 or 1' >&2
  exit 1
fi
if [ "$NATIVE_SDHCI" = 1 ]; then
  PERSONALITY_CFLAGS="$PERSONALITY_CFLAGS -DXNU_NATIVE_SDHCI=1"
fi'''),
    # 5. 归档链接（放在 pthread 块之前，与 ramblock 同级）
    ('''  if [ "${XNU_SOURCE_PTHREAD:-0}" = 1 ]; then''',
     '''  if [ "$NATIVE_SDHCI" = 1 ]; then
    SDHCI_ARCHIVE=$ROOT/build/native-sdhci/libNativeSdhci.a
    test -f "$SDHCI_ARCHIVE"
    KERNEL_EXTRA_ARCHIVES="$KERNEL_EXTRA_ARCHIVES -Wl,-force_load,$SDHCI_ARCHIVE"
    KERNEL_ARCHIVE_DEPENDENCIES="$KERNEL_ARCHIVE_DEPENDENCIES $SDHCI_ARCHIVE"
  fi
  if [ "${XNU_SOURCE_PTHREAD:-0}" = 1 ]; then'''),
], 'build-xnu-4k-baseline.sh')

print('完成：NativeSdhci 已接入构建（编译 + 链接两个环节）')
