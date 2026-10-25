#!/usr/bin/env python3
"""让 vcputc_options 在"控制台被禁用"时先调用 gc_enable(TRUE)，再继续绘制。

vcputc_options 开头：

    gc_initialized = [0xFFFFFFF007B2EE00]
    gc_enabled     = [0xFFFFFFF007B2EE04]
    若 (gc_initialized == 1 && gc_enabled == 0) → 直接 return（什么都不画）

我们把这 5 条"收尾指令"（0x73B78B8..0x73B78C8）改写成：

    mov  x19, x0            ; 保存字符参数
    mov  w0, #1
    bl   gc_enable          ; 分配缓冲区 + 置 gc_initialized/gc_enabled
    mov  x0, x19            ; 恢复字符参数
    b    0x73b78d8          ; 继续走正常绘制流程

用法: python3 patch-gc-enable-call.py <kernel.macho> [输出]
"""
import struct
import sys
from pathlib import Path

BASE = 0xFFFFFFF0073B78B8
# (va, 期望原指令, 新指令)
PATCHES = [
    (0xFFFFFFF0073B78B8, 0xA9587BFD, 0xAA0003F3),   # mov x19, x0
    (0xFFFFFFF0073B78BC, 0xA9574FF4, 0x52800020),   # mov w0, #1
    (0xFFFFFFF0073B78C0, 0xA95657F6, 0x940010E0),   # bl gc_enable
    (0xFFFFFFF0073B78C4, 0xA9555FF8, 0xAA1303E0),   # mov x0, x19
    (0xFFFFFFF0073B78C8, 0xA95467FA, 0x14000004),   # b 0x73b78d8
]


def va_to_off(d: bytes, va: int):
    _, _, _, _, ncmds, _, _ = struct.unpack_from('<IiiIIII', d, 0)
    off = 32
    for _ in range(ncmds):
        cmd, cmdsize = struct.unpack_from('<II', d, off)
        if cmd == 0x19:
            name = d[off + 8:off + 24].rstrip(b'\0').decode(errors='replace')
            vmaddr, vmsize, fileoff, filesize = struct.unpack_from('<QQQQ', d, off + 24)
            if vmaddr <= va < vmaddr + filesize:
                return fileoff + (va - vmaddr), name
        off += cmdsize
    return None, None


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    src = Path(sys.argv[1])
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else src
    d = bytearray(src.read_bytes())

    ok = True
    for va, expect, new in PATCHES:
        off, seg = va_to_off(d, va)
        if off is None:
            print(f'!! 0x{va:x} 不在文件内')
            ok = False
            continue
        cur = struct.unpack_from('<I', d, off)[0]
        if cur == new:
            print(f'0x{va:x} 已打过补丁')
            continue
        if cur != expect:
            print(f'!! 0x{va:x} 指令不匹配: 0x{cur:08x} != 0x{expect:08x}')
            ok = False
            continue
        struct.pack_into('<I', d, off, new)
        print(f'0x{va:x} (段 {seg}): 0x{expect:08x} -> 0x{new:08x}')

    if not ok:
        print('未写入')
        return 1
    dst.write_bytes(d)
    print(f'输出: {dst}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
