#!/usr/bin/env python3
"""把内核里的 _gc_enabled 置 1（图形控制台启用），让 vcputc_options 不再提前返回。

vcputc_options 开头：
    gc_initialized = [0xFFFFFFF007B2EE00]      （字节）
    gc_enabled     = [0xFFFFFFF007B2EE04]      （字）
    若 (gc_initialized == 1 && gc_enabled == 0) → 直接 return，一个字都不画

用法: python3 patch-gc-enabled.py <kernel.macho> [输出]
"""
import struct
import sys
from pathlib import Path

GC_ENABLED_VA = 0xFFFFFFF007B2EE04
KERNEL_BASE = 0xFFFFFFF007004000
FILE_BASE = 0x1000


def va_to_off(d: bytes, va: int):
    _, _, _, _, ncmds, _, _ = struct.unpack_from('<IiiIIII', d, 0)
    off = 32
    for _ in range(ncmds):
        cmd, cmdsize = struct.unpack_from('<II', d, off)
        if cmd == 0x19:
            name = d[off + 8:off + 24].rstrip(b'\0').decode(errors='replace')
            vmaddr, vmsize, fileoff, filesize = struct.unpack_from('<QQQQ', d, off + 24)
            if vmaddr <= va < vmaddr + vmsize:
                return fileoff + (va - vmaddr), name, vmsize
        off += cmdsize
    return None, None, None


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    src = Path(sys.argv[1])
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else src
    d = bytearray(src.read_bytes())

    off, seg, _ = va_to_off(d, GC_ENABLED_VA)
    if off is None:
        off = GC_ENABLED_VA - KERNEL_BASE + FILE_BASE
        seg = '(估算)'
        print(f'! 用估算偏移 0x{off:x}')

    cur = struct.unpack_from('<I', d, off)[0]
    print(f'gc_enabled @ 0x{GC_ENABLED_VA:x} 段 {seg}: 当前 0x{cur:08x}')
    struct.pack_into('<I', d, off, 1)
    print('已置为 1')

    # 顺便确认 gc_initialized 附近的状态（打印邻居字节，便于判断段是否可写）
    for va, label in ((0xFFFFFFF007B2EE00, 'gc_initialized'), (0xFFFFFFF007B2F08C, 'gc_buffer_size')):
        o2, seg2, _ = va_to_off(d, va)
        if o2 is None:
            print(f'{label}: 不在文件里（BSS）')
        else:
            print(f'{label} @ 0x{va:x} 段 {seg2}: 文件值 0x{struct.unpack_from("<I", d, o2)[0]:08x}')

    dst.write_bytes(d)
    print(f'输出: {dst}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
