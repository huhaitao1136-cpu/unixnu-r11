#!/usr/bin/env python3
"""让 vcputc_options 在"控制台被禁用"时把 gc_enabled 置 1 再继续绘制。

运行时实测：
    gc_initialized = 1   (0xFFFFFFF007B2EE00)
    gc_enabled     = 0   (0xFFFFFFF007B2EE04)   ← 导致每次直接 return

把提前返回的 5 条收尾指令改成：

    0x73B78B8:  adrp x9, 0xFFFFFFF007B2E000
    0x73B78BC:  mov  w10, #1
    0x73B78C0:  str  w10, [x9, #0xE04]      ; gc_enabled = 1
    0x73B78C4:  b    0x73B78D8              ; 继续正常绘制流程
    0x73B78C8:  (保留)

只写内存，不调用任何函数，因此不会像上次那样死锁；x0（字符参数）也不受影响。

用法: python3 patch-gc-enabled-code.py <kernel.macho> [输出]
"""
import struct
import sys
from pathlib import Path

TARGET_VA   = 0xFFFFFFF007B2EE04
TARGET_PAGE = TARGET_VA & ~0xFFF
STR_OFF     = TARGET_VA & 0xFFF

# (va, 期望原指令, 新指令占位 None=后面算)
PATCH_VA = [
    0xFFFFFFF0073B78B8,
    0xFFFFFFF0073B78BC,
    0xFFFFFFF0073B78C0,
    0xFFFFFFF0073B78C4,
]
EXPECT = [0xA9587BFD, 0xA9574FF4, 0xA95657F6, 0xA9555FF8]
BRANCH_TARGET = 0xFFFFFFF0073B78D8


def adrp_encode(from_va: int, target_page: int, reg: int) -> int:
    from_page = from_va & ~0xFFF
    delta = (target_page - from_page) >> 12
    assert -(1 << 20) <= delta < (1 << 20), 'adrp 超范围'
    imm = delta & 0x1FFFFF
    immlo = imm & 0x3
    immhi = (imm >> 2) & 0x7FFFF
    return 0x90000000 | (immlo << 29) | (immhi << 5) | reg


def encode_str_u32(base_reg: int, off: int, src_reg: int) -> int:
    assert off % 4 == 0
    imm12 = off >> 2
    return 0xB9000000 | ((imm12 & 0xFFF) << 10) | (base_reg << 5) | src_reg


def encode_b(from_va: int, to_va: int) -> int:
    delta = to_va - from_va
    assert delta % 4 == 0
    imm = (delta >> 2) & 0x3FFFFFF
    return 0x14000000 | imm


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


def va_to_off(segs, va):
    for name, vm, vs, fo, fs in segs:
        if vm <= va < vm + fs:
            return fo + (va - vm)
    return None


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    src = Path(sys.argv[1])
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else src
    d = bytearray(src.read_bytes())
    segs = segments(d)

    new = [
        adrp_encode(PATCH_VA[0], TARGET_PAGE, 9),          # adrp x9, page
        0x52800000 | (1 << 5) | 10,                        # mov w10, #1
        encode_str_u32(9, STR_OFF, 10),                    # str w10, [x9, #0xE04]
        encode_b(PATCH_VA[3], BRANCH_TARGET),              # b main_path
    ]

    for va, expect, ins in zip(PATCH_VA, EXPECT, new):
        off = va_to_off(segs, va)
        if off is None:
            print(f'!! 0x{va:x} 不在文件内')
            return 1
        cur = struct.unpack_from('<I', d, off)[0]
        if cur != expect:
            print(f'!! 0x{va:x} 指令不匹配: 0x{cur:08x} != 0x{expect:08x}')
            return 1
        struct.pack_into('<I', d, off, ins)
        print(f'0x{va:x}: 0x{expect:08x} -> 0x{ins:08x}')

    dst.write_bytes(d)
    print(f'输出: {dst}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
