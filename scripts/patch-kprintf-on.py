#!/usr/bin/env python3
"""把内核里的调试开关字节置 0（打开 kprintf / 串口 / IOLog 输出）。

默认地址（kernel.macho 符号表）：
  _disable_serial_output        0xFFFFFFF0071F9B50
  _disable_kprintf_output       0xFFFFFFF0071F9B51
  _disable_iolog_serial_output  0xFFFFFFF0071F9B52

用法:
  python3 patch-kprintf-on.py <输入 macho> <输出 macho> [地址...]
"""
import struct
import sys
from pathlib import Path

DEFAULTS = [
    0xFFFFFFF0071F9B50,   # disable_serial_output
    0xFFFFFFF0071F9B51,   # disable_kprintf_output
    0xFFFFFFF0071F9B52,   # disable_iolog_serial_output
]


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
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    addrs = [int(v, 0) for v in sys.argv[3:]] or DEFAULTS

    d = bytearray(src.read_bytes())
    segs = segments(d)

    for va in addrs:
        done = False
        for name, vm, vs, fo, fs in segs:
            if vm <= va < vm + vs:
                f = fo + (va - vm)
                old = d[f]
                d[f] = 0
                print(f'0x{va:x}  段 {name}  文件偏移 0x{f:x}  {old} -> 0')
                done = True
                break
        if not done:
            print(f'!! 0x{va:x} 不在任何段内')

    dst.write_bytes(d)
    print(f'输出: {dst} ({dst.stat().st_size} B)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
