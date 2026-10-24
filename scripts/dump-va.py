#!/usr/bin/env python3
"""按虚拟地址从 macho 里 dump 一段字节到文件（供 objdump 反汇编）。

用法:
  python3 dump-va.py <kernel.macho> <起始va> <长度> <输出.bin>
例:
  python3 dump-va.py /tmp/kernel-pan5.macho 0xFFFFFFF004986d00 0x300 /tmp/panic.bin
"""
import struct
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 5:
        print(__doc__)
        return 2
    macho = Path(sys.argv[1])
    va = int(sys.argv[2], 0)
    length = int(sys.argv[3], 0)
    out = Path(sys.argv[4])

    d = macho.read_bytes()
    _, _, _, _, ncmds, _, _ = struct.unpack_from('<IiiIIII', d, 0)
    off = 32
    segs = []
    for _ in range(ncmds):
        cmd, cmdsize = struct.unpack_from('<II', d, off)
        if cmd == 0x19:  # LC_SEGMENT_64
            name = d[off + 8:off + 24].rstrip(b'\0').decode(errors='replace')
            vmaddr, vmsize, fileoff, filesize = struct.unpack_from('<QQQQ', d, off + 24)
            segs.append((name, vmaddr, vmsize, fileoff, filesize))
        off += cmdsize

    for name, vm, vs, fo, fs in segs:
        if vm <= va < vm + vs:
            f = fo + (va - vm)
            data = d[f:f + length]
            out.write_bytes(data)
            print(f'段 {name}: va 0x{va:x} -> 文件偏移 0x{f:x}，dump {len(data)} 字节到 {out}')
            print('字节:', data[:32].hex())
            return 0

    print('该 va 不在任何段内。段表：')
    for s in segs:
        print(f'  {s[0]:20s} vmaddr=0x{s[1]:x} vmsize=0x{s[2]:x} fileoff=0x{s[3]:x}')
    return 1


if __name__ == '__main__':
    sys.exit(main())
