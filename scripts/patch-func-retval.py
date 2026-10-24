#!/usr/bin/env python3
"""把某个函数开头改成 "mov w0, #<val>; ret"。

用法:
  python3 patch-func-retval.py <输入> <输出> <函数va> [返回值=0]
例:
  python3 patch-func-retval.py a.macho b.macho 0xFFFFFFF004986AC4 1
"""
import struct
import sys
from pathlib import Path

RET = struct.pack('<I', 0xD65F03C0)


def segments(d: bytes):
    _, _, _, _, ncmds, _, _ = struct.unpack_from('<IiiIIII', d, 0)
    off = 32
    segs = []
    for _ in range(ncmds):
        cmd, cmdsize = struct.unpack_from('<II', d, off)
        if cmd == 0x19:
            name = d[off + 8:off + 24].rstrip(b'\0').decode(errors='replace')
            vmaddr, vmsize, fileoff, filesize = struct.unpack_from('<QQQQ', d, off + 24)
            segs.append((name, vmaddr, vmsize, fileoff, filesize))
        off += cmdsize
    return segs


def main() -> int:
    if len(sys.argv) < 4:
        print(__doc__)
        return 2
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    va = int(sys.argv[3], 0)
    val = int(sys.argv[4], 0) if len(sys.argv) > 4 else 0
    if not 0 <= val <= 0xFFFF:
        print('返回值需在 0..0xFFFF')
        return 1

    d = bytearray(src.read_bytes())
    mov_w0 = struct.pack('<I', 0x52800000 | (val & 0xFFFF))
    for name, vm, vs, fo, fs in segments(d):
        if vm <= va < vm + vs:
            f = fo + (va - vm)
            print(f'0x{va:x} 段 {name} 偏移 0x{f:x}: '
                  f'{d[f:f+8].hex()} -> mov w0,#{val}; ret')
            d[f:f + 4] = mov_w0
            d[f + 4:f + 8] = RET
            dst.write_bytes(d)
            print(f'输出: {dst} ({dst.stat().st_size} B)')
            return 0
    print(f'!! 0x{va:x} 不在任何段内')
    return 1


if __name__ == '__main__':
    sys.exit(main())
