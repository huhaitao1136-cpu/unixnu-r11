#!/usr/bin/env python3
"""把 macho 里某个函数的开头改成 "mov w0,#0; ret"（返回 false）。

用途：快速验证某个 kext / 函数是不是 panic 元凶——让它直接返回失败，
IOKit 就会跳过它，看内核能不能继续往下走。

用法:
  python3 patch-func-ret0.py <输入 macho> <输出 macho> <函数va> [函数va...]
例:
  python3 patch-func-ret0.py a.macho b.macho 0xFFFFFFF004A803EC
"""
import struct
import sys
from pathlib import Path

MOV_W0_0 = struct.pack('<I', 0x52800000)   # mov w0, #0
RET = struct.pack('<I', 0xD65F03C0)        # ret


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
    vas = [int(v, 0) for v in sys.argv[3:]]
    d = bytearray(src.read_bytes())
    segs = segments(d)

    for va in vas:
        done = False
        for name, vm, vs, fo, fs in segs:
            if vm <= va < vm + vs:
                f = fo + (va - vm)
                d[f:f + 4] = MOV_W0_0
                d[f + 4:f + 8] = RET
                print(f'0x{va:x} 位于 {name}，文件偏移 0x{f:x} -> mov w0,#0; ret')
                done = True
                break
        if not done:
            print(f'!! 0x{va:x} 不在任何段内')

    dst.write_bytes(d)
    print(f'输出: {dst} ({dst.stat().st_size} B)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
