#!/usr/bin/env python3
"""把 R11 需要的所有 XNU 内核补丁一次打全。

补丁清单:
  1. PAN（ARMv8.1）→ NOP / MOV wN,#1            骁龙660 是 v8.0
  2. AppleARMCPU::_validateConfiguration 返回值 → 恒真
  3. disable_serial_output / disable_kprintf_output / disable_iolog_serial_output → 0
  4. serial_init() → 恒返回 false（PE_kputc 回退到视频控制台）
  5. cons_ops_index → 1（视频控制台）

用法:
  python3 patch-kernel-full.py <输入 kernel.macho> <输出 kernel.macho>
"""
import struct
import sys
from pathlib import Path

NOP = struct.pack('<I', 0xD503201F)
MOV_W0_0 = 0x52800000
RET = 0xD65F03C0

VALIDATE_RET = 0xFFFFFFF0049871A0      # and w0,w19,#1
SYM_DISABLE = [
    0xFFFFFFF0071F9B50,                # disable_serial_output
    0xFFFFFFF0071F9B51,                # disable_kprintf_output
    0xFFFFFFF0071F9B52,                # disable_iolog_serial_output
]
SYM_SERIAL_INIT = 0xFFFFFFF00793E724
SYM_CONS_OPS_INDEX = 0xFFFFFFF007167920


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


def va_to_off(segs, va):
    for name, vm, vs, fo, fs in segs:
        if vm <= va < vm + vs:
            return fo + (va - vm), name
    return None, None


def patch_pan(d: bytearray) -> int:
    n = 0
    for i in range(0, len(d) - 4, 4):
        w = struct.unpack_from('<I', d, i)[0]
        if w == 0xD500419F:
            d[i:i + 4] = NOP; n += 1
        elif (w & 0xFFFFFFE0) == 0xD5384260:
            d[i:i + 4] = struct.pack('<I', 0x52800020 | (w & 0x1F)); n += 1
        elif w == 0xE7FFDEFF:
            d[i:i + 4] = NOP; n += 1
    return n


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    d = bytearray(src.read_bytes())
    segs = segments(d)

    n = patch_pan(d)
    print(f'1) PAN 指令: {n} 条')

    off, seg = va_to_off(segs, VALIDATE_RET)
    if off is not None:
        old = struct.unpack_from('<I', d, off)[0]
        struct.pack_into('<I', d, off, 0x52800020)
        print(f'2) CPU 校验返回值: 0x{old:08x} -> mov w0,#1')
    else:
        print('2) !! 找不到 CPU 校验点')

    cnt = 0
    for sym in SYM_DISABLE:
        off, _ = va_to_off(segs, sym)
        if off is not None:
            d[off] = 0
            cnt += 1
    print(f'3) 输出开关: {cnt}/3 置 0')

    off, seg = va_to_off(segs, SYM_SERIAL_INIT)
    if off is not None:
        struct.pack_into('<I', d, off, MOV_W0_0)
        struct.pack_into('<I', d, off + 4, RET)
        print(f'4) serial_init -> mov w0,#0; ret (段 {seg})')
    else:
        print('4) !! 找不到 serial_init')

    off, seg = va_to_off(segs, SYM_CONS_OPS_INDEX)
    if off is not None:
        old = struct.unpack_from('<I', d, off)[0]
        struct.pack_into('<I', d, off, 1)
        print(f'5) cons_ops_index: {old} -> 1 (视频控制台, 段 {seg})')
    else:
        print('5) !! 找不到 cons_ops_index')

    dst.write_bytes(d)
    print(f'输出: {dst} ({dst.stat().st_size} B)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
