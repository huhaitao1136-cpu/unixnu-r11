#!/usr/bin/env python3
"""扫描 Mach-O 里所有 bl 指令，找出指向某个函数地址的调用点（xref）。

用法:
  python3 find-xrefs.py <kernel.macho> <目标VA> [段名]

例:
  python3 find-xrefs.py kernel.macho 0xFFFFFFF0073B9EC8
  python3 find-xrefs.py kernel.macho 0xFFFFFFF0073BBC40
"""
import struct
import sys
from pathlib import Path


def segments(d: bytes):
    _, _, _, _, ncmds, _, _ = struct.unpack_from('<IiiIIII', d, 0)
    off = 32
    out = []
    for _ in range(ncmds):
        cmd, cmdsize = struct.unpack_from('<II', d, off)
        if cmd == 0x19:
            name = d[off + 8:off + 24].rstrip(b'\0').decode(errors='replace')
            vmaddr, vmsize, fileoff, filesize = struct.unpack_from('<QQQQ', d, off + 24)
            out.append((name, vmaddr, vmsize, fileoff, filesize))
        off += cmdsize
    return out


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    macho = Path(sys.argv[1])
    target = int(sys.argv[2], 0)
    only_seg = sys.argv[3] if len(sys.argv) > 3 else '__TEXT_EXEC'
    d = macho.read_bytes()

    hits = []
    for name, vm, vs, fo, fs in segments(d):
        if only_seg and name != only_seg:
            continue
        blob = d[fo:fo + fs]
        for i in range(0, len(blob) - 4, 4):
            ins = struct.unpack_from('<I', blob, i)[0]
            if (ins & 0xFC000000) != 0x94000000:      # bl
                continue
            imm = ins & 0x3FFFFFF
            if imm & 0x2000000:
                imm -= 0x4000000                       # sign-extend 26-bit
            dest = vm + i + (imm << 2)
            if dest == target:
                hits.append((vm + i, name))

    print(f'目标 0x{target:012x} 被 bl 调用的次数: {len(hits)}')
    for va, seg in hits:
        print(f'  调用点 0x{va:012x}  ({seg})')
    return 0


if __name__ == '__main__':
    sys.exit(main())
