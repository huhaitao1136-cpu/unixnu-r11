#!/usr/bin/env python3
"""把 NativeSdhci 接入内核构建（同时改源码仓库与已装配的树）。

用法（在主仓库根目录，或 working-directory 指向它）：
    python3 device/scripts/patch-tree-for-sdhci.py

改动内容：
  build-arm-driver.py
    1. 组件 choices 加入 'NativeSdhci'
    2. 输出目录 'NativeSdhci' -> build/native-sdhci
    3. 存储 ABI 头文件组加入 'NativeSdhci'（IOBlockStorageDevice.h 来自 build/storage-abi）
  build-xnu-4k-baseline.sh
    4. 新增 NATIVE_SDHCI 开关，打开时 -DXNU_NATIVE_SDHCI=1
    5. 打开时把 libNativeSdhci.a 用 -Wl,-force_load 链进内核

说明：build-arm-driver.py 来自 SoC 仓库，只在装配树 out/<dev>/<os>/<cfg>/tree/scripts
里出现；build-xnu-4k-baseline.sh 在主仓库 scripts 与装配树里都有。这里「发现即改」，
两处都改，避免 build 时重新装配覆盖。脚本可重复执行（已改过就跳过）。
"""
from pathlib import Path
import sys


def find_root() -> Path:
    """定位 UniXNU 主仓库根目录（含 scripts/build-xnu-4k-baseline.sh）。"""
    here = Path.cwd()
    for candidate in [here, *here.parents]:
        if (candidate / 'scripts' / 'build-xnu-4k-baseline.sh').is_file():
            return candidate
        tree = candidate / 'UniXNU'
        if (tree / 'scripts' / 'build-xnu-4k-baseline.sh').is_file():
            return tree
    sys.exit('找不到 UniXNU 主仓库（scripts/build-xnu-4k-baseline.sh）；'
             '请在主仓库根目录或用 working-directory: UniXNU 执行')


ROOT = find_root()
print(f'主仓库根目录: {ROOT}')

DRIVER_EDITS = [
    ("'NativeUfs', 'NativeHidInput',",
     "'NativeUfs', 'NativeSdhci', 'NativeHidInput',"),
    ("'NativeUfs':'native-ufs', 'NativeHidInput':'native-hid-input',",
     "'NativeUfs':'native-ufs', 'NativeSdhci':'native-sdhci', 'NativeHidInput':'native-hid-input',"),
    ("if component in ('NativeVirtioBlock', 'NativeRamBlock', 'NativeUfs'):",
     "if component in ('NativeVirtioBlock', 'NativeRamBlock', 'NativeUfs', 'NativeSdhci'):"),
]

KERNEL_EDITS = [
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
    ('''  if [ "${XNU_SOURCE_PTHREAD:-0}" = 1 ]; then
    PTHREAD_ARCHIVE=$ROOT/build/libpthread-kernel/libpthread_kernel.a''',
     '''  if [ "$NATIVE_SDHCI" = 1 ]; then
    SDHCI_ARCHIVE=$ROOT/build/native-sdhci/libNativeSdhci.a
    test -f "$SDHCI_ARCHIVE"
    KERNEL_EXTRA_ARCHIVES="$KERNEL_EXTRA_ARCHIVES -Wl,-force_load,$SDHCI_ARCHIVE"
    KERNEL_ARCHIVE_DEPENDENCIES="$KERNEL_ARCHIVE_DEPENDENCIES $SDHCI_ARCHIVE"
  fi
  if [ "${XNU_SOURCE_PTHREAD:-0}" = 1 ]; then
    PTHREAD_ARCHIVE=$ROOT/build/libpthread-kernel/libpthread_kernel.a'''),
]


def patch(path: Path, edits, label: str) -> bool:
    if not path.is_file():
        return False
    text = path.read_text()
    original = text
    for old, new in edits:
        if new in text:
            continue                       # 已经改过
        if old not in text:
            sys.exit(f'{label} ({path}): 找不到锚点，上游可能变了：{old.splitlines()[0][:70]!r}')
        text = text.replace(old, new, 1)
    if text != original:
        path.write_text(text)
        print(f'  {label}: 已修补  {path}')
    else:
        print(f'  {label}: 无需改动  {path}')
    return True


driver_targets = [ROOT / 'scripts' / 'build-arm-driver.py']
kernel_targets = [ROOT / 'scripts' / 'build-xnu-4k-baseline.sh']
for scripts in sorted(ROOT.glob('out/*/*/*/tree/scripts')):
    driver_targets.append(scripts / 'build-arm-driver.py')
    kernel_targets.append(scripts / 'build-xnu-4k-baseline.sh')

touched = 0
print('build-arm-driver.py:')
for t in dict.fromkeys(driver_targets):
    touched += bool(patch(t, DRIVER_EDITS, 'build-arm-driver.py'))
print('build-xnu-4k-baseline.sh:')
for t in dict.fromkeys(kernel_targets):
    touched += bool(patch(t, KERNEL_EDITS, 'build-xnu-4k-baseline.sh'))

if not touched:
    sys.exit('两个脚本都没找到；检查工作目录')

print('完成：NativeSdhci 已接入构建（编译 + 链接两个环节）')
