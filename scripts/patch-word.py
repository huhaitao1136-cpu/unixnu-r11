#!/usr/bin/env python3
"""把 macho 里指定虚拟地址处的 4 字节指令替换成任意编码。

用法:
  python3 patch-word.py <输入> <输出> <va> <新指令十六进制> [数量]
例:
  python3 patch-word.py a.macho b.macho 0xFFFFFFF0049871A0 52800020
  （把 and w0,w19,#1 换成 mov w0,#1）
"""
import struct
import sys
from pathlib import Path


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
    if len(sys.argv) < 5:
        print(__doc__)
        return 2
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    va = int(sys.argv[3], 0)
    word = int(sys.argv[4], 16)
    count = int(sys.argv[5]) if len(sys.argv) > 5 else 1

    data = bytearray(src.read_bytes())
    for name, vm, vs, fo, fs in segments(data):
        if vm <= va < vm + vs:
            f = fo + (va - vm)
            for i in range(count):
                old = struct.unpack_from('<I', data, f + 4 * i)[0]
                print(f'0x{va + 4*i:x}: 0x{old:08x} -> 0x{word:08x}')
                struct.pack_into('<I', data, f + 4 * i, word)
            dst.write_bytes(data)
            print(f'输出: {dst} ({dst.stat().st_size} B)')
            return 0
    print(f'!! 0x{va:x} 不在任何段内')
    return 1


if __name__ == '__main__':
    sys.exit(main())
