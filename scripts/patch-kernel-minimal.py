#!/usr/bin/env python3
"""让内核能在 ARMv8.0 + 视频控制台上跑起来的最小补丁集。

历史教训（2026-10-27）
----------------------
这个脚本以前把 6 个地址写死：

    SYM_DISABLE       = [0xFFFFFFF0071F9B50, ...51, ...52]
    SYM_SERIAL_INIT   = 0xFFFFFFF00793E724
    SYM_CONS_OPS_INDEX= 0xFFFFFFF007167920

内核一重新链接就全漂了。用符号表一对，这版内核里它们其实在：

    符号                     正确地址          硬编码值落在哪
    _disable_serial_output   0x71FDB50         __ZTV17IOPowerConnection（C++ vtable！）
    _disable_kprintf_output  0x71FDB51         同上
    _disable_iolog_...       0x71FDB52         同上
    _serial_init             0x79424DC         __GLOBAL__sub_I_IOPMrootDomain.cpp
    _cons_ops_index          0x716B920         _zone_security_array

也就是说旧脚本把 3 个 vtable 字节清零、把某个静态构造函数的头两条指令改成
`mov w0,#0; ret`、还往 zone 安全数组里写了 1 —— 结果内核跑到
`iBoot Stage 2 version:` 之后就死了。

所以现在必须传 `--symbols`（llvm-nm 输出），全部按符号定位：

  1) PAN：全域按指令编码扫描（`msr PAN` 两种编码）
  2) CPU 校验：按内容断言（`and w0, w19, #1` -> `mov w0, #1`）
  3) mrs xN, PAN / 0xE7FFDEFF：全域按编码扫描
  4) 三个输出开关：按符号
  5) serial_init：按符号，写 `mov w0,#0; ret`
  6) cons_ops_index：按符号，写 1（视频控制台）

用法:
  python3 patch-kernel-minimal.py <in> <out> --symbols <nm.txt> \
      [--with-validate] [--with-trapfix] [--with-mrs1|--with-mrs0] [--with-cpu-fakes]
"""
import struct
import sys
from pathlib import Path

NOP = struct.pack('<I', 0xD503201F)
MOV_W0_0 = 0x52800000
RET = 0xD65F03C0

VALIDATE_VA = 0xFFFFFFF0049871A0
VALIDATE_OLD = 0x12000260          # and w0, w19, #1

SYM_SERIAL_INIT = '_serial_init'
SYM_CONS_OPS_INDEX = '_cons_ops_index'
SYM_DISABLE = ['_disable_serial_output', '_disable_kprintf_output',
               '_disable_iolog_serial_output']

# 找不到符号时绝不退回硬编码地址：那只会去改坏别人的 vtable。
ALLOW_FALLBACK = '--allow-fallback-addresses' in sys.argv


def segments(d: bytes):
    ncmds = struct.unpack_from('<I', d, 16)[0]
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


def parse_symbols(path):
    table = {}
    for line in Path(path).read_text(errors='replace').splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        try:
            addr = int(parts[0], 16)
        except ValueError:
            continue
        name = parts[2]
        table.setdefault(name, addr)
        if name.startswith('_'):
            table.setdefault(name[1:], addr)
        else:
            table.setdefault('_' + name, addr)
    return table


def patch_pan(d: bytearray) -> int:
    n = 0
    for i in range(0, len(d) - 4, 4):
        w = struct.unpack_from('<I', d, i)[0]
        if w in (0xD500409F, 0xD500419F):
            d[i:i + 4] = NOP
            n += 1
    return n


def main() -> int:
    argv = sys.argv[1:]
    symbols = None
    if '--symbols' in argv:
        k = argv.index('--symbols')
        symbols = argv[k + 1]
        del argv[k:k + 2]
    if len(argv) < 2:
        print(__doc__)
        return 2
    src, dst = Path(argv[0]), Path(argv[1])
    with_validate = '--with-validate' in argv or '--with-cpu-fakes' in argv
    with_fakes = '--with-cpu-fakes' in argv or '--with-feature-fakes' in argv
    with_mrs = '--with-mrs1' in argv or '--with-mrs0' in argv or with_fakes
    mrs_value = 1 if '--with-mrs1' in argv else 0
    with_trap = '--with-trapfix' in argv or with_fakes

    d = bytearray(src.read_bytes())
    segs = segments(d)
    syms = parse_symbols(symbols) if symbols else {}
    if not symbols:
        print('!! 没传 --symbols：脚本无法安全定位 serial_init / cons_ops_index / '
              '输出开关（硬编码地址已经漂过），先停。')

    print(f'1) PAN 指令: {patch_pan(d)} 条 → nop')

    if with_validate:
        off, seg = va_to_off(segs, VALIDATE_VA)
        if off is None:
            print('2) 找不到 CPU 校验点，跳过')
        else:
            old = struct.unpack_from('<I', d, off)[0]
            if old == VALIDATE_OLD:
                struct.pack_into('<I', d, off, 0x52800020)
                print(f'2) CPU 校验返回值 @0x{VALIDATE_VA:x}: 0x{old:08x} -> mov w0,#1')
            else:
                print(f'2) 0x{VALIDATE_VA:x} 内容是 0x{old:08x}，不是预期的 '
                      f'0x{VALIDATE_OLD:08x}，跳过（地址可能也漂了）')
    else:
        print('2) 跳过 CPU 校验伪造')

    m_ok = m_bad = 0
    for i in range(0, len(d) - 4, 4):
        w = struct.unpack_from('<I', d, i)[0]
        if (w & 0xFFFFFFE0) == 0xD5384260 and with_mrs:
            d[i:i + 4] = struct.pack('<I', 0x52800000 | (mrs_value << 5) | (w & 0x1F))
            m_ok += 1
        elif w == 0xE7FFDEFF and with_trap:
            d[i:i + 4] = NOP
            m_bad += 1
    print(f'3a) mrs xN,PAN → mov wN,#{mrs_value}: {m_ok} 处' + ('' if with_mrs else '（跳过）'))
    print(f'3b) 0xE7FFDEFF trap → nop: {m_bad} 处' + ('' if with_trap else '（跳过）'))

    cnt = 0
    for name in SYM_DISABLE:
        va = syms.get(name) or syms.get(name.lstrip('_'))
        if va is None:
            print(f'   !! 符号缺失: {name}')
            continue
        off, seg = va_to_off(segs, va)
        if off is None:
            print(f'   !! {name}=0x{va:x} 不在任何段里')
            continue
        d[off] = 0
        cnt += 1
        print(f'   {name} @0x{va:x} -> 0')
    print(f'4) 输出开关: {cnt}/{len(SYM_DISABLE)} 置 0')

    va = syms.get(SYM_SERIAL_INIT) or syms.get(SYM_SERIAL_INIT.lstrip('_'))
    if va is None:
        print(f'5) !! 符号缺失: {SYM_SERIAL_INIT}，跳过')
    else:
        off, seg = va_to_off(segs, va)
        if off is None:
            print(f'5) !! {SYM_SERIAL_INIT}=0x{va:x} 不在任何段里')
        else:
            old = struct.unpack_from('<I', d, off)[0]
            struct.pack_into('<I', d, off, MOV_W0_0)
            struct.pack_into('<I', d, off + 4, RET)
            print(f'5) {SYM_SERIAL_INIT} @0x{va:x}: 0x{old:08x} -> mov w0,#0; ret')

    va = syms.get(SYM_CONS_OPS_INDEX) or syms.get(SYM_CONS_OPS_INDEX.lstrip('_'))
    if va is None:
        print(f'6) !! 符号缺失: {SYM_CONS_OPS_INDEX}，跳过')
    else:
        off, seg = va_to_off(segs, va)
        if off is None:
            print(f'6) !! {SYM_CONS_OPS_INDEX}=0x{va:x} 不在任何段里')
        else:
            old = struct.unpack_from('<I', d, off)[0]
            struct.pack_into('<I', d, off, 1)
            print(f'6) {SYM_CONS_OPS_INDEX} @0x{va:x}: {old} -> 1（视频控制台）')

    dst.write_bytes(d)
    print(f'输出: {dst} ({dst.stat().st_size} B)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
