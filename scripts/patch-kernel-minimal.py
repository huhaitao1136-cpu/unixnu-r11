#!/usr/bin/env python3
"""最小内核补丁：只打"让内核能在 ARMv8.0 + 视频控制台跑起来"的必需项。

与 patch-kernel-full.py 的区别：
  * 保留：PAN 指令（两种编码都处理）、输出开关、serial_init、cons_ops_index
  * 跳过：AppleARMCPU::_validateConfiguration 返回值伪造
  * 跳过：mrs xN,<sysreg> → mov wN,#1（伪造 CPU 特性读取）
  * 跳过：0xE7FFDEFF 特殊标记 → NOP

这些被跳过的补丁可能让内核相信某些不存在的 CPU 能力，进而影响
文件系统/缓存路径。用最小补丁可以对照验证。

用法:
  python3 patch-kernel-minimal.py <输入 kernel.macho> <输出 kernel.macho> [--with-cpu-fakes]
"""
import struct
import sys
from pathlib import Path

NOP = struct.pack('<I', 0xD503201F)
MOV_W0_0 = 0x52800000
RET = 0xD65F03C0

VALIDATE_RET = 0xFFFFFFF0049871A0
SYM_DISABLE = [0xFFFFFFF0071F9B50, 0xFFFFFFF0071F9B51, 0xFFFFFFF0071F9B52]
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
    """两种 PAN 编码都换成等价的 ARMv8.0 指令。"""
    n = 0
    for i in range(0, len(d) - 4, 4):
        w = struct.unpack_from('<I', d, i)[0]
        if w == 0xD500409F:
            # msr PAN, #0（关闭保护）→ 无 PAN 的 CPU 上本来就是关闭的，等价于 nop
            d[i:i + 4] = NOP
            n += 1
        elif w == 0xD500419F:
            # msr PAN, #1（开启保护）→ 无 PAN 的 CPU 无法支持，忽略
            d[i:i + 4] = NOP
            n += 1
    return n


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    with_validate = '--with-validate' in sys.argv or '--with-cpu-fakes' in sys.argv
    with_fakes = '--with-cpu-fakes' in sys.argv or '--with-feature-fakes' in sys.argv
    with_mrs = '--with-mrs1' in sys.argv or '--with-mrs0' in sys.argv or with_fakes
    mrs_value = 1 if '--with-mrs1' in sys.argv else 0
    with_trap = '--with-trapfix' in sys.argv or with_fakes
    d = bytearray(src.read_bytes())
    segs = segments(d)

    n = patch_pan(d)
    print(f'1) PAN 指令: {n} 条 → nop')

    if with_validate:
        off, seg = va_to_off(segs, VALIDATE_RET)
        if off is not None:
            old = struct.unpack_from('<I', d, off)[0]
            struct.pack_into('<I', d, off, 0x52800020)
            print(f'2) CPU 校验返回值: 0x{old:08x} -> mov w0,#1')
    else:
        print('2) 跳过 CPU 校验伪造')

    m_ok = m_bad = 0
    for i in range(0, len(d) - 4, 4):
        w = struct.unpack_from('<I', d, i)[0]
        if (w & 0xFFFFFFE0) == 0xD5384260 and with_mrs:
            # mrs xN, PAN → mov wN, #<mrs_value>
            # A72 没有 PAN：读出来应当表示"保护未启用"（0）
            d[i:i + 4] = struct.pack('<I', 0x52800000 | (mrs_value << 5) | (w & 0x1F))
            m_ok += 1
        elif w == 0xE7FFDEFF and with_trap:
            d[i:i + 4] = NOP
            m_bad += 1
    print(f'3a) mrs xN,PAN → mov wN,#{mrs_value}: {m_ok} 处' + ('' if with_mrs else '（跳过）'))
    print(f'3b) 0xE7FFDEFF trap → nop: {m_bad} 处' + ('' if with_trap else '（跳过）'))

    cnt = 0
    for sym in SYM_DISABLE:
        off, _ = va_to_off(segs, sym)
        if off is not None:
            d[off] = 0
            cnt += 1
    print(f'4) 输出开关: {cnt}/3 置 0')

    off, seg = va_to_off(segs, SYM_SERIAL_INIT)
    if off is not None:
        struct.pack_into('<I', d, off, MOV_W0_0)
        struct.pack_into('<I', d, off + 4, RET)
        print(f'5) serial_init -> mov w0,#0; ret')

    off, seg = va_to_off(segs, SYM_CONS_OPS_INDEX)
    if off is not None:
        old = struct.unpack_from('<I', d, off)[0]
        struct.pack_into('<I', d, off, 1)
        print(f'6) cons_ops_index: {old} -> 1（视频控制台）')

    dst.write_bytes(d)
    print(f'输出: {dst} ({dst.stat().st_size} B)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
