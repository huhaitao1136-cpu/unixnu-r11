#!/usr/bin/env python3
"""在 initialize_screen 的尾声挂钩：先 initialize_screen 正常返回，再 gc_enable(TRUE)。

为什么不再用固定地址
--------------------
旧版把两个 VA 写死：

    RET_VA       = 0xFFFFFFF0073BA7C8   # initialize_screen 的 ret
    GC_ENABLE_VA = 0xFFFFFFF0073BBC40   # gc_enable 的入口

内核每次重新链接这些地址都会漂。v20 那轮 `0x73BA7C8` 已经变成
`ldr w8,[x19]`（0xb9400268），补丁直接失败，payload 于是没带上屏补丁，
内核一个字都打不出来、屏幕停在 UniXNU 菜单帧。

现在改成按符号表定位（`llvm-nm` 的 `<addr> <type> <name>` 输出）：

  1. 取 `_initialize_screen` 与 `_gc_enable` 的地址；
  2. 函数范围 = `[_initialize_screen, 下一个符号)`；
  3. 在范围里找 `ret`（0xD65F03C0），取**最后一个**（尾声）；
  4. 在 `__TEXT_EXEC` 的 NOP 空洞里写 20 字节跳板：

        stp  x29, x30, [sp, #-16]!
        mov  w0, #1
        bl   _gc_enable
        ldp  x29, x30, [sp], #16
        ret

  5. 把尾声那条 `ret` 改成 `b 跳板`。

语义与旧版完全一样：initialize_screen 的正文跑完、栈帧已弹开，然后
gc_enable(TRUE)，再真正返回。因为挂在函数尾声，所有调用者都会经过它。

用法:
  python3 patch-init-screen-gc.py <kernel.macho> [输出] --symbols <nm.txt>
"""
import struct
import sys
from pathlib import Path

NOP = b'\x1f\x20\x03\xd5'
RET = 0xD65F03C0


def segments(d):
    """解析 Mach-O 的 LC_SEGMENT_64：[(name, vmaddr, vmsize, fileoff, filesize)]。"""
    ncmds = struct.unpack_from('<I', d, 16)[0]
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
    if abs(delta) >= (1 << 27):
        raise ValueError('bl 超出 ±128MB')
    return 0x94000000 | ((delta >> 2) & 0x3FFFFFF)


def enc_b(from_va, to_va):
    delta = to_va - from_va
    if abs(delta) >= (1 << 27):
        raise ValueError('b 超出 ±128MB')
    return 0x14000000 | ((delta >> 2) & 0x3FFFFFF)


def find_last_cave(d, segs, need=20):
    """在 __TEXT_EXEC 里找最后一段 >= need 字节的 NOP 空洞。"""
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


def parse_symbols(path):
    table = []
    for line in Path(path).read_text(errors='replace').splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        try:
            addr = int(parts[0], 16)
        except ValueError:
            continue
        table.append((addr, parts[2]))
    table.sort()
    return table


def symbol_addr(table, wanted):
    for addr, name in table:
        if name == wanted or name == '_' + wanted or name.lstrip('_') == wanted.lstrip('_'):
            return addr
    return None


def next_symbol(table, addr):
    for a, _ in table:
        if a > addr:
            return a
    return None


def main() -> int:
    argv = sys.argv[1:]
    symbols = None
    if '--symbols' in argv:
        k = argv.index('--symbols')
        symbols = argv[k + 1]
        del argv[k:k + 2]
    if not argv:
        print(__doc__)
        return 2
    if not symbols:
        print('!! 需要 --symbols <llvm-nm 输出>（固定地址模式已废弃）')
        return 2

    src = Path(argv[0])
    dst = Path(argv[1]) if len(argv) > 1 else src
    d = bytearray(src.read_bytes())
    segs = segments(d)
    table = parse_symbols(symbols)

    init = symbol_addr(table, '_initialize_screen')
    gc = symbol_addr(table, '_gc_enable')
    if not init or not gc:
        print(f'!! 符号缺失: initialize_screen={init} gc_enable={gc}')
        return 1
    end = next_symbol(table, init)
    print(f'符号: initialize_screen=0x{init:x} gc_enable=0x{gc:x} 函数结束=0x{end:x}')
    if end - init > 0x10000:
        print('!! initialize_screen 范围过大，符号表可能不全')
        return 1

    o = va_to_off(segs, init)
    if o is None:
        print('!! initialize_screen 不在任何段里')
        return 1
    rets = []
    for i in range((end - init) // 4):
        if struct.unpack_from('<I', d, o + 4 * i)[0] == RET:
            rets.append(init + 4 * i)
    if not rets:
        print('!! 函数体里找不到 ret')
        return 1
    print(f'  函数体里 {len(rets)} 个 ret，用最后一个 0x{rets[-1]:x}')
    ret_va = rets[-1]
    roff = va_to_off(segs, ret_va)

    cave = find_last_cave(d, segs)
    if cave is None:
        print('!! 找不到 >=20 字节空洞')
        return 1
    print(f'空洞: 0x{cave:x}')
    coff = va_to_off(segs, cave)

    insns = [
        0xA9BF7BFD,                 # stp x29, x30, [sp, #-16]!
        0x52800020,                 # mov w0, #1
        enc_bl(cave + 8, gc),       # bl gc_enable
        0xA8C17BFD,                 # ldp x29, x30, [sp], #16
        0xD65F03C0,                 # ret
    ]
    for k, ins in enumerate(insns):
        struct.pack_into('<I', d, coff + 4 * k, ins)
    print(f'  跳板写入 {len(insns) * 4} 字节')

    cur = struct.unpack_from('<I', d, roff)[0]
    if cur != RET:
        print(f'!! 0x{ret_va:x} 不是 ret (0x{cur:08x})')
        return 1
    new = enc_b(ret_va, cave)
    struct.pack_into('<I', d, roff, new)
    print(f'  0x{ret_va:x}: ret -> b 跳板(0x{new:08x})')

    dst.write_bytes(d)
    print(f'输出: {dst}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
