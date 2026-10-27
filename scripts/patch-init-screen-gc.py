#!/usr/bin/env python3
"""在 initialize_screen 被调用的地方插一个跳板：先调 initialize_screen，
再调 gc_enable(TRUE)。

为什么不再用固定地址：VAs 会随内核布局漂移（v20 那轮内核一变，
0x73BA7C8 就不是 `ret` 了，补丁直接失败）。现在改成按符号表定位：

  1. --symbols 给 llvm-nm 的输出（`<addr> <type> <name>`，名字带前导下划线）
  2. 找到 `_initialize_screen` 与 `_gc_enable` 的地址
  3. 在 `__TEXT_EXEC` 里（优先限定在 `_PE_init_platform` 范围内）找
     `bl _initialize_screen` 那条指令，把它改成 `bl 跳板`
  4. 跳板（__TEXT_EXEC 的 NOP 空洞里，24 字节）：
        stp  x29, x30, [sp, #-16]!
        bl   _initialize_screen
        mov  w0, #1
        bl   _gc_enable
        ldp  x29, x30, [sp], #16
        ret

不管是从 PE_init_platform 调还是别处调，语义都正确（等于"包一层"，时序仍在
initialize_screen 之后、单线程、vinfo 已完整）。

用法:
  python3 patch-init-screen-gc.py <kernel.macho> [输出] --symbols <nm.txt>
"""
import struct
import sys
from pathlib import Path

NOP = b'\x1f\x20\x03\xd5'


def segments(d):
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
    if abs(delta) >= (1 << 27):
        raise ValueError('bl 超出 ±128MB')
    return 0x94000000 | ((delta >> 2) & 0x3FFFFFF)


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
        if name == wanted or name == '_' + wanted:
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
    print(f'符号: initialize_screen=0x{init:x} gc_enable=0x{gc:x}')

    # 优先在 _PE_init_platform 范围内找调用点
    callers = []
    for fname in ('_PE_init_platform', '_IOService::initialize', '_kernel_bootstrap'):
        base = symbol_addr(table, fname) if fname.startswith('_') else None
        if base:
            callers.append((fname, base, next_symbol(table, base)))
            break

    def scan(lo, hi):
        found = []
        o = va_to_off(segs, lo)
        if o is None:
            return found
        end = min(hi, lo + 96 * 1024 * 1024)
        n = (end - lo) // 4
        for i in range(n):
            ins = struct.unpack_from('<I', d, o + 4 * i)[0]
            if (ins & 0xFC000000) != 0x94000000:
                continue
            imm = ins & 0x3FFFFFF
            if imm & 0x2000000:
                imm -= 0x4000000
            if lo + 4 * i + imm * 4 == init:
                found.append(lo + 4 * i)
        return found

    sites = []
    if callers:
        name, lo, hi = callers[0]
        print(f'在 {name} (0x{lo:x}..0x{hi:x}) 里找 bl initialize_screen')
        sites = scan(lo, hi)
    if not sites:
        print('退回到整个 __TEXT_EXEC 扫描')
        for nm, vm, vs, fo, fs in segs:
            if nm == '__TEXT_EXEC':
                sites = scan(vm, vm + fs)
                break
    if not sites:
        print('!! 找不到 bl initialize_screen')
        return 1
    for s in sites:
        print(f'  调用点 0x{s:x}')

    site = sites[0]
    if len(sites) > 1:
        print(f'  有 {len(sites)} 处调用，只改第一处')

    cave = find_last_cave(d, segs)
    if cave is None:
        print('!! 找不到 >=24 字节空洞')
        return 1
    print(f'空洞: 0x{cave:x}')
    coff = va_to_off(segs, cave)
    if coff is None:
        print('!! 空洞不在文件内')
        return 1

    insns = [
        0xA9BF7BFD,                     # stp x29, x30, [sp, #-16]!
        enc_bl(cave + 4, init),         # bl initialize_screen
        0x52800020,                     # mov w0, #1
        enc_bl(cave + 12, gc),          # bl gc_enable
        0xA8C17BFD,                     # ldp x29, x30, [sp], #16
        0xD65F03C0,                     # ret
    ]
    for k, ins in enumerate(insns):
        struct.pack_into('<I', d, coff + 4 * k, ins)
    print(f'  跳板写入 {len(insns) * 4} 字节')

    soff = va_to_off(segs, site)
    cur = struct.unpack_from('<I', d, soff)[0]
    if (cur & 0xFC000000) != 0x94000000:
        print(f'!! 0x{site:x} 不是 bl (0x{cur:08x})')
        return 1
    new = enc_bl(site, cave)
    struct.pack_into('<I', d, soff, new)
    print(f'  0x{site:x}: bl initialize_screen(0x{cur:08x}) -> bl 跳板(0x{new:08x})')

    dst.write_bytes(d)
    print(f'输出: {dst}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
