#!/usr/bin/env python3
"""刷机前的廉价校验：内核里到底有没有 NativeSdhci 这个类、有没有它的 IOKit personality。

用法：
    python verify-kernel-personality.py <kernel.macho 或含 kernel.macho 的 zip 或目录>

检查项：
  1. 内核构建时间串（确认拿的是新一轮 CI 的产物）
  2. 'NativeSdhci' 类名（force_load 链进来才有）
  3. "'IOClass'         = NativeSdhci" personality 串（KernelConfigTables.cpp 里那段才有）
  4. IOKitPersonalities 出现次数（Apple 自带的计数，做对照）
  5. 顺带查 NativeUfs / NativeAES 作对照：这两个是已知会出现在上游内核里的

判定：
  类名有、personality 串无  -> 补丁没投递/没应用，别刷机，先修 CI
  两者都有                  -> 可以刷，期待屏幕出现 NativeSdhci: matched DT node
  类名也无                  -> 驱动归档没链进去
"""
import pathlib
import sys
import zipfile


MARKERS = {
    'NativeSdhci class': b'NativeSdhci',
    'NativeSdhci personality': b"'IOClass'         = NativeSdhci",
    'NativeUfs personality': b"'IOClass'         = NativeUfs",
    'NativeAES personality': b"'IOClass'         = NativeAES",
    'IOKitPersonalities': b'IOKitPersonalities',
    'kernel version': b'Darwin Kernel Version ',
}


def find_kernel(argument: str) -> bytes:
    path = pathlib.Path(argument)
    if path.is_dir():
        for candidate in sorted(path.rglob('kernel.macho')):
            print(f'读取 {candidate}')
            return candidate.read_bytes()
        sys.exit(f'{path} 下没有 kernel.macho')
    if path.suffix.lower() == '.zip':
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                if name.endswith('kernel.macho'):
                    print(f'从 {path.name} 读取 {name}')
                    return archive.read(name)
        sys.exit(f'{path} 里没有 kernel.macho')
    print(f'读取 {path}')
    return path.read_bytes()


def main() -> int:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    data = find_kernel(sys.argv[1])
    print(f'大小 {len(data):,} B')
    if data[:4] not in (b'\xcf\xfa\xed\xfe', b'\xce\xfa\xed\xfe'):
        print('警告：开头不是 Mach-O magic，可能不是内核文件')

    at = data.find(MARKERS['kernel version'])
    if at >= 0:
        tail = data[at:at + 96].split(b'\x00')[0].decode('latin-1', 'replace')
        print(f'构建串: {tail}')
    else:
        print('构建串: 没找到（可疑）')

    counts = {}
    for label, needle in MARKERS.items():
        if label in ('kernel version',):
            continue
        counts[label] = data.count(needle)
        print(f'{label:<26} {counts[label]} 次')

    klass = counts['NativeSdhci class'] > 0
    personality = counts['NativeSdhci personality'] > 0
    print()
    if klass and personality:
        print('结论：可以刷。类名和 personality 都在，probe 有被调用的条件。')
        return 0
    if klass and not personality:
        print('结论：不要刷。类链进来了，但 personality 没进内核 —— '
              'patch-tree-for-sdhci.py 的补丁投递没生效（查 CI 日志里是否打印了 personality patch 已写入）。')
        return 1
    print('结论：不要刷。连 NativeSdhci 类名都没有 —— '
          'libNativeSdhci.a 没被 -force_load 链进内核（查 archive 7/10 与 SDHCI_ARCHIVE 那段）。')
    return 1


if __name__ == '__main__':
    sys.exit(main())
