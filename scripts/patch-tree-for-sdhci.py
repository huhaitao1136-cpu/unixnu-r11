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
  patches/xnu/native-sdhci-personality.patch
    6. 把 XNU_NATIVE_SDHCI 的 IOKit personality 写进主仓库 patches/xnu/ 与装配树
       （内核的 gIOKernelConfigTables 只在编译期烧进去；没有这段，NativeSdhci 类
       永远不会被 IOKit 匹配到，probe 也不会被调用）

注意：不能把 .patch 直接放在设备仓库 patches/ 下——tree.py 把 patches 当模块整目录
链接，同名的 xnu 模块会盖掉主仓库那套上游补丁。所以这里在构建期把文件投递过去。

说明：build-arm-driver.py 来自 SoC 仓库，只在装配树 out/<dev>/<os>/<cfg>/tree/scripts
里出现；build-xnu-4k-baseline.sh 在主仓库 scripts 与装配树里都有。这里「发现即改」，
两处都改，避免 build 时重新装配覆盖。脚本可重复执行（已改过就跳过）。
"""
from pathlib import Path
import subprocess
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
    # SDHCI personality 插在 native-aes-personality.patch 新增块的正后方（都是 ")"; 前的 #endif）。
    # 只要它留在树里，AES 补丁的 `--reverse --check` 就会失败、正向 apply 也失败，构建直接 exit。
    # 所以把它并进 LATE_PATCH 先撤一轮，下面按名字顺序重新应用。
    ('''for LATE_PATCH in "$ROOT/patches/xnu/zzz-boot-progress.patch" "$ROOT/patches/xnu/zzy-staged-system-apps.patch" \\
    "$ROOT/patches/xnu/zzy-native-gpu-addon-mount.patch"; do''',
     '''for LATE_PATCH in "$ROOT/patches/xnu/zzz-boot-progress.patch" "$ROOT/patches/xnu/zzy-staged-system-apps.patch" \\
    "$ROOT/patches/xnu/zzy-native-gpu-addon-mount.patch" \\
    "$ROOT/patches/xnu/native-sdhci-personality.patch"; do'''),
]


PERSONALITY_PATCH = Path(__file__).resolve().parent / 'data' / 'native-sdhci-personality.patch'


def install_personality_patch() -> None:
    """把 personality 补丁投递到主仓库 patches/xnu/ 与每个已装配的树。"""
    if not PERSONALITY_PATCH.is_file():
        sys.exit(f'缺少 {PERSONALITY_PATCH}')
    text = PERSONALITY_PATCH.read_text()
    targets = [ROOT / 'patches' / 'xnu', *[t / 'patches' / 'xnu' for t in assembled_trees()]]
    for folder in dict.fromkeys(targets):
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / PERSONALITY_PATCH.name
        if target.is_file() and target.read_text() == text:
            print(f'  personality patch: 已存在  {target}')
            continue
        with target.open('w', encoding='utf-8', newline='\n') as handle:
            handle.write(text)
        print(f'  personality patch: 已写入  {target}')


def assembled_trees():
    """已装配的树：实际布局是 out/<dev>/tree；老布局 out/<dev>/<os>/<cfg>/tree 也一并兼容。"""
    found = [*sorted(ROOT.glob('out/*/tree')), *sorted(ROOT.glob('out/*/*/*/tree'))]
    return [d for d in dict.fromkeys(found) if d.is_dir()]


def xnu_work_dirs():
    """所有可能出现过的 XNU 源码缓存（build-xnu-4k-baseline.sh 用的是相对 .cache/xnu-work）。"""
    dirs = [ROOT / '.cache' / 'xnu-work']
    for tree in assembled_trees():
        dirs.append(tree / '.cache' / 'xnu-work')
    return [d for d in dict.fromkeys(dirs) if (d / 'iokit' / 'KernelConfigTables.cpp').is_file()]


def revert_personality_patch() -> None:
    """把上一轮构建留在 .cache/xnu-work 里的 SDHCI personality 插入撤回。

    build-xnu-4k-baseline.sh 靠 `git apply --reverse --check` 判断"是否已应用"来跳过补丁。
    我们的插入点紧跟在 native-aes-personality.patch 新增块的末尾（都是 ")"; 之前的
    `#endif`），所以一旦我们的块留在树里，AES 补丁的反向检查就会失败 →
    构建在 `patch failed: iokit/KernelConfigTables.cpp:61` 处直接 exit。
    因此每次构建前先撤回自己，让构建脚本按名字顺序（aes 在前、sdhci 在后）重新应用。"""
    patch_file = ROOT / 'patches' / 'xnu' / PERSONALITY_PATCH.name
    if not patch_file.is_file():
        return
    for work in xnu_work_dirs():
        check = subprocess.run(
            ['git', '-C', str(work), 'apply', '--reverse', '--check', str(patch_file)],
            capture_output=True, text=True)
        if check.returncode != 0:
            continue
        subprocess.run(['git', '-C', str(work), 'apply', '--reverse', str(patch_file)],
                       check=True, capture_output=True, text=True)
        print(f'  personality patch: 已撤回旧插入  {work}')


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


# build-arm-driver.py 来自设备仓库（已在仓库里改好）；这里只兜底检查
driver_targets = [ROOT / 'scripts' / 'build-arm-driver.py', ROOT.parent / 'device' / 'scripts' / 'build-arm-driver.py']
kernel_targets = [ROOT / 'scripts' / 'build-xnu-4k-baseline.sh']
for scripts in [t / 'scripts' for t in assembled_trees()]:
    driver_targets.append(scripts / 'build-arm-driver.py')
    kernel_targets.append(scripts / 'build-xnu-4k-baseline.sh')

touched = 0
print('build-arm-driver.py:')
for t in dict.fromkeys(driver_targets):
    touched += bool(patch(t, DRIVER_EDITS, 'build-arm-driver.py'))
print('build-xnu-4k-baseline.sh:')
for t in dict.fromkeys(kernel_targets):
    touched += bool(patch(t, KERNEL_EDITS, 'build-xnu-4k-baseline.sh'))

print('patches/xnu/native-sdhci-personality.patch:')
install_personality_patch()
revert_personality_patch()

if not touched:
    sys.exit('两个脚本都没找到；检查工作目录')

print('完成：NativeSdhci 已接入构建（编译 + 链接 + personality 三段）')
