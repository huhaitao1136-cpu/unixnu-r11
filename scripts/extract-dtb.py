#!/usr/bin/env python3
"""从 Android boot 镜像（v0）里提取 DTB。

Android boot v0 布局：
  kernel     = gzip(BootShim + FD)
  kernel_size + ramdisk_size 之后跟着 DTB

用法:
  python3 extract-dtb.py <boot.img> [输出.dtb]
"""
import struct
import sys
from pathlib import Path

FDT_MAGIC = b'\xd0\x0d\xfe\xed'


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    img = Path(sys.argv[1])
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else img.with_suffix('.dtb')
    d = img.read_bytes()
    print(f'镜像 {img.name}: {len(d)} B')

    kernel_size, ramdisk_size = struct.unpack_from('<II', d, 8)
    print(f'kernel_size={kernel_size} ramdisk_size={ramdisk_size}')

    starts = []
    i = 0
    while True:
        i = d.find(FDT_MAGIC, i)
        if i < 0:
            break
        size = struct.unpack_from('>I', d, i + 4)[0]
        starts.append((i, size))
        i += 4

    if not starts:
        print('!! 没找到 DTB（FDT magic）')
        return 1

    print(f'找到 {len(starts)} 个 FDT 候选:')
    best = None
    for off, size in starts:
        ok = 0x1000 < size < 0x200000
        print(f'  偏移 0x{off:x} 大小 {size} {"✓" if ok else "?"}')
        if ok and (best is None or size > best[1]):
            best = (off, size)

    if best is None:
        return 1
    off, size = best
    out.write_bytes(d[off:off + size])
    print(f'已写出 {out} ({size} B)')
    print(f'反编译: dtc -I dtb -O dts -o {out.with_suffix(".dts")} {out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
