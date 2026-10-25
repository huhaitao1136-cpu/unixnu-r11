#!/usr/bin/env python3
"""在 __TEXT_EXEC 的代码空洞里放一个 trampoline，让启动早期调用 gc_enable(TRUE)。

trampoline（16 字节）:
    mov  w0, #1
    bl   gc_enable          ; 分配图形控制台缓冲区并置标志
    bl   serial_init        ; 补回被替换掉的调用
    ret

调用点:
    PE_init_kprintf (0xFFFFFFF007A1EFF8)
      0xA1F000: bl serial_init   →   bl trampoline

这样 gc_enable 在"启动早期、无锁"的上下文里被调用一次。

用法: python3 patch-gc-init-trampoline.py <kernel.macho> [输出]
"""
import struct
import sys
from pathlib import Path

NOP = b'\x1f\x20\x03\xd5'
CALLSITE_VA = 0xFFFFFFF007A1F000
CALLSITE_EXPECT = 0x97FC7DC9      # bl 0xfffffff00793e724 (serial_init)

SERIAL_INIT_VA = 0xFFFFFFF00793E724
GC_ENABLE_VA   = 0xFFFFFFF0073BBC40


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


def bl_encode(from_va: int, to_va: int) -> int:
    delta = to_va - from_va
    assert delta % 4 == 0, '目标未对齐'
    imm = (delta >> 2) & 0x3FFFFFF
    return 0x94000000 | imm


def find_last_cave(d: bytes, segs, need=24):
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
                    best = vm + i        # 取最后一个（最高地址）
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

    cave = find_last_cave(d, segs, 24)
    if cave is None:
        print('!! 找不到 >=16 字节的 NOP 空洞')
        return 1
    print(f'空洞: 0x{cave:x}')

    off = va_to_off(segs, cave)
    if off is None:
        print('!! 空洞地址不在文件内')
        return 1

    insns = [
        0xA9BF7BFD,                                   # stp x29, x30, [sp, #-16]!
        0x52800020,                                   # mov w0, #1
        bl_encode(cave + 8,  GC_ENABLE_VA),           # bl gc_enable
        bl_encode(cave + 12, SERIAL_INIT_VA),         # bl serial_init
        0xA8C17BFD,                                   # ldp x29, x30, [sp], #16
        0xD65F03C0,                                   # ret
    ]
    for k, ins in enumerate(insns):
        struct.pack_into('<I', d, off + 4 * k, ins)
        print(f'  0x{cave + 4 * k:x}: 0x{ins:08x}')

    # 调用点：PE_init_kprintf 里那条 bl serial_init
    coff = va_to_off(segs, CALLSITE_VA)
    if coff is None:
        print('!! 调用点不在文件内')
        return 1
    cur = struct.unpack_from('<I', d, coff)[0]
    print(f'调用点 0x{CALLSITE_VA:x}: 0x{cur:08x} (期望 0x{CALLSITE_EXPECT:08x})')
    if cur != CALLSITE_EXPECT:
        print('!! 调用点指令不匹配，未修改调用点')
        return 1
    new = bl_encode(CALLSITE_VA, cave)
    struct.pack_into('<I', d, coff, new)
    print(f'  改为 bl 0x{cave:x} (0x{new:08x})')

    dst.write_bytes(d)
    print(f'输出: {dst}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
