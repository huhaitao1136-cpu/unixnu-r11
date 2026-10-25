#!/usr/bin/env python3
"""给内核打"强制初始化 gc_ops"的补丁。

initialize_screen 里：

    0x73BA270:  cmp  w19, #0x8            ; op == kPEBaseAddressChange
    0x73BA274:  adrp x22, gc_ops
    0x73BA278:  b.eq 0x73BA3EC            ; ★ 相等就跳过 gc_ops 的全部赋值
    0x73BA27C:  adrp x8, gc_ops
    0x73BA280:  adr  x9, _vc_update_color
    0x73BA288:  str  x9, [x8, #40]

把 b.eq 改成 nop，让 gc_ops.paint_char 等函数指针一定被填上，
vcputc 才能真的把字画到帧缓冲。

用法: python3 patch-gcops.py <输入 kernel.macho> [输出 kernel.macho]
"""
import struct
import sys
from pathlib import Path

TARGET_VA = 0xFFFFFFF0073BA278
EXPECT = 0x54000BA0        # b.eq 0xfffffff0073ba3ec
NOP = 0xD503201F

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
        # 退回按文件偏移估算
        off = TARGET_VA - KERNEL_BASE + FILE_BASE
        print(f'! 用估算偏移 0x{off:x}')

    cur = struct.unpack_from('<I', d, off)[0]
    print(f'0x{TARGET_VA:x} 当前指令 0x{cur:08x} (期望 0x{EXPECT:08x})')
    if cur != EXPECT:
        print('!! 指令不匹配，未修改')
        return 1
    struct.pack_into('<I', d, off, NOP)
    print(f'已改为 NOP（文件偏移 0x{off:x}）')

    dst.write_bytes(d)
    print(f'输出: {dst} ({dst.stat().st_size} B)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
