#!/usr/bin/env python3
"""在 initialize_screen 返回前调用 gc_enable(TRUE)。

initialize_screen (0xFFFFFFF0073B9EC8) 的主 epilogue：

    0x73BA7C8:  ret

改成  b trampoline，跳板（放在 __TEXT_EXEC 的 NOP 空洞里）做：

    stp  x29, x30, [sp, #-16]!
    mov  w0, #1
    bl   gc_enable
    ldp  x29, x30, [sp], #16
    ret

时机：initialize_screen 已被 PE_init_platform 调用、vinfo 完整、
单线程、未持有控制台锁 —— 最安全的初始化点。

用法: python3 patch-init-screen-gc.py <kernel.macho> [输出]
"""
import struct
import sys
from pathlib import Path

NOP = b'\x1f\x20\x03\xd5'
RET_VA = 0xFFFFFFF0073BA7C8
RET_EXPECT = 0xD65F03C0
GC_ENABLE_VA = 0xFFFFFFF0073BBC40


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


def enc_bl(from_va, to_va):
    delta = to_va - from_va
    imm = (delta >> 2) & 0x3FFFFFF
    return 0x94000000 | imm


def enc_b(from_va, to_va):
    delta = to_va - from_va
    imm = (delta >> 2) & 0x3FFFFFF
    return 0x14000000 | imm


def find_last_cave(d, segs, need=24):
    best = None
    for name, vm, vs, fo, fs in segs:
        if name != '__TEXT_EXEC':
            continue
        blob = d[fo:fo + fs]
        i = 0
        while i < len(blob) - 4:
            if blob[i:i + 4] == NOP:
                j = i
                while j < len(blob) - 4 and blob[j:j + 4] == NOP:
                    j += 4
                if (j - i) >= need:
                    best = vm + i
                i = j
            else:
                i += 4
    return best


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    src = Path(sys.argv[1])
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else src
    d = bytearray(src.read_bytes())
    segs = segments(d)

    cave = find_last_cave(d, segs)
    if cave is None:
        print('!! 找不到 >=24 字节空洞')
        return 1
    print(f'空洞: 0x{cave:x}')

    coff = va_to_off(segs, cave)
    insns = [
        0xA9BF7BFD,                       # stp x29, x30, [sp, #-16]!
        0x52800020,                       # mov w0, #1
        enc_bl(cave + 8, GC_ENABLE_VA),   # bl gc_enable
        0xA8C17BFD,                       # ldp x29, x30, [sp], #16
        0xD65F03C0,                       # ret
    ]
    for k, ins in enumerate(insns):
        struct.pack_into('<I', d, coff + 4 * k, ins)
    print(f'  跳板写入 {len(insns) * 4} 字节')

    roff = va_to_off(segs, RET_VA)
    if roff is None:
        print('!! ret 地址不在文件内')
        return 1
    cur = struct.unpack_from('<I', d, roff)[0]
    print(f'0x{RET_VA:x}: 0x{cur:08x} (期望 0x{RET_EXPECT:08x})')
    if cur != RET_EXPECT:
        print('!! 指令不匹配')
        return 1
    new = enc_b(RET_VA, cave)
    struct.pack_into('<I', d, roff, new)
    print(f'  ret -> b 0x{cave:x} (0x{new:08x})')

    dst.write_bytes(d)
    print(f'输出: {dst}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
