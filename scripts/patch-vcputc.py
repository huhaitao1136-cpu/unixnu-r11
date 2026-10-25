#!/usr/bin/env python3
"""强制 vcputc_options 走绘制主流程（跳过开头的提前返回）。

vcputc_options (0xFFFFFFF0073B787C) 开头：

    0x73b78a0:  ldrb w8, [x8, #3584]      ; 标志 A
    0x73b78a8:  ldr  w9, [x20, #3588]     ; 标志 B
    0x73b78ac:  cmp  w8, #0x1
    0x73b78b0:  ccmp w9, #0x0, #0x4, eq
    0x73b78b4:  b.ne 0x73b78d8            ; (A != 1) 或 (B != 0) → 主流程
                ; 否则 ret（直接返回，一个字都不画）

把 b.ne 改成无条件 b 0x73b78d8，保证一定进入绘制路径。

用法: python3 patch-vcputc.py <kernel.macho> [输出]
"""
import struct
import sys
from pathlib import Path

TARGET_VA = 0xFFFFFFF0073B78B4
EXPECT    = 0x54000121        # b.ne 0xfffffff0073b78d8
REPLACE   = 0x14000009        # b    0xfffffff0073b78d8

KERNEL_BASE = 0xFFFFFFF007004000
FILE_BASE = 0x1000


def va_to_off(d: bytes, va: int):
    _, _, _, _, ncmds, _, _ = struct.unpack_from('<IiiIIII', d, 0)
    off = 32
    for _ in range(ncmds):
        cmd, cmdsize = struct.unpack_from('<II', d, off)
        if cmd == 0x19:
            vmaddr, vmsize, fileoff, filesize = struct.unpack_from('<QQQQ', d, off + 24)
            if vmaddr <= va < vmaddr + vmsize:
                return fileoff + (va - vmaddr)
        off += cmdsize
    return None


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    src = Path(sys.argv[1])
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else src
    d = bytearray(src.read_bytes())

    off = va_to_off(d, TARGET_VA)
    if off is None:
        off = TARGET_VA - KERNEL_BASE + FILE_BASE
        print(f'! 用估算偏移 0x{off:x}')

    cur = struct.unpack_from('<I', d, off)[0]
    print(f'0x{TARGET_VA:x}: 0x{cur:08x} (期望 0x{EXPECT:08x})')
    if cur != EXPECT:
        if cur == REPLACE:
            print('已经打过补丁')
            return 0
        print('!! 指令不匹配，未修改')
        return 1

    struct.pack_into('<I', d, off, REPLACE)
    dst.write_bytes(d)
    print(f'已改为无条件跳转（偏移 0x{off:x}），输出 {dst}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
