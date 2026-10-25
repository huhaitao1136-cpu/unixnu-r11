#!/usr/bin/env python3
"""静态验证一个最小 ARM64 Mach-O：头、加载命令、段/节、入口、指令与跳转目标。

用法: python3 verify-macho.py <init.macho>
退出码 0 表示全部检查通过。
"""
import struct
import sys
from pathlib import Path

LC_NAMES = {
    0x1: 'LC_SEGMENT', 0x2: 'LC_SYMTAB', 0x5: 'LC_UNIXTHREAD', 0xB: 'LC_DYSYMTAB',
    0x19: 'LC_SEGMENT_64', 0x1B: 'LC_UUID', 0xC: 'LC_LOAD_DYLIB',
    0x80000028: 'LC_MAIN', 0x26: 'LC_FUNCTION_STARTS',
}

COND = ['eq', 'ne', 'cs', 'cc', 'mi', 'pl', 'vs', 'vc',
        'hi', 'ls', 'ge', 'lt', 'gt', 'le', 'al', 'nv']


def decode(word: int, pc: int) -> str:
    if word & 0x7C000000 == 0x14000000:
        imm = word & 0x3FFFFFF
        if imm & 0x2000000:
            imm -= 0x4000000
        return f'b {pc + imm * 4:#x}'
    if word & 0xFF000010 == 0x54000000:
        imm = (word >> 5) & 0x7FFFF
        if imm & 0x40000:
            imm -= 0x80000
        return f'b.{COND[word & 0xF]} {pc + imm * 4:#x}'
    if word & 0x9F000000 == 0x10000000:
        rd = word & 0x1F
        immlo = (word >> 29) & 0x3
        immhi = (word >> 5) & 0x7FFFF
        imm = (immhi << 2) | immlo
        if imm & (1 << 20):
            imm -= 1 << 21
        return f'adr x{rd}, {pc + imm:#x}'
    if word & 0x7F800000 == 0x52800000:
        return f'movz w{word & 0x1F}, #{((word >> 5) & 0xFFFF):#x}'
    if word & 0x7F800000 == 0xD2800000:
        return f'movz x{word & 0x1F}, #{((word >> 5) & 0xFFFF):#x}'
    if word & 0xFFE0FFE0 == 0xAA0003E0:
        return f'mov x{word & 0x1F}, x{(word >> 16) & 0x1F}'
    if word & 0xFFE0001F == 0xD4000001:
        return f'svc #{(word >> 5) & 0xFFFF:#x}'
    return f'.inst {word:#010x}'


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    p = Path(sys.argv[1])
    b = p.read_bytes()
    ok = True

    def check(cond, msg):
        nonlocal ok
        print(('  OK   ' if cond else '  FAIL ') + msg)
        if not cond:
            ok = False

    print(f'文件: {p}  ({len(b)} B)')
    magic, cpu, sub, ftype, ncmds, sizeofcmds, flags, _res = struct.unpack_from('<IIIIIIII', b, 0)
    check(magic == 0xFEEDFACF, f'magic = {magic:#x} (期望 0xfeedfacf, 64 位)')
    check(cpu == 0x0100000C, f'cputype = {cpu:#x} (ARM64)')
    check(sub == 0x2, f'cpusubtype = {sub:#x}')
    check(ftype == 2, f'filetype = {ftype} (MH_EXECUTE)')
    check(ncmds == 2, f'ncmds = {ncmds}')
    print(f'  sizeofcmds = {sizeofcmds}')

    off = 32
    text = None
    pc = None
    for i in range(ncmds):
        cmd, cmdsize = struct.unpack_from('<II', b, off)
        name = LC_NAMES.get(cmd, f'{cmd:#x}')
        print(f'  [{i}] {name} (cmdsize={cmdsize})')
        if cmd == 0x19:
            (_c, _s, segname, vmaddr, vmsize, fileoff, filesize,
             maxprot, initprot, nsects, _fl) = struct.unpack_from('<II16sQQQQIIII', b, off)
            segname = segname.rstrip(b'\0').decode()
            print(f'        seg={segname} vmaddr={vmaddr:#x} vmsize={vmsize:#x} '
                  f'fileoff={fileoff:#x} filesize={filesize:#x} initprot={initprot}')
            check(segname == '__TEXT', '段名是 __TEXT')
            check(fileoff == 0, 'fileoff = 0（段从文件头开始）')
            check(initprot & 0x4 and initprot & 0x1, 'initprot 可读可执行')
            check(filesize <= len(b), f'filesize {filesize:#x} 不超过文件长度 {len(b):#x}')
            check(vmsize >= filesize, 'vmsize >= filesize')
            s = off + 72
            for _ in range(nsects):
                (sectname, s_seg, addr, size, s_off, align, reloff, nreloc,
                 s_flags, r1, r2, r3) = struct.unpack_from('<16s16sQQIIIIIIII', b, s)
                sectname = sectname.rstrip(b'\0').decode()
                s_seg = s_seg.rstrip(b'\0').decode()
                print(f'        sect={s_seg},{sectname} addr={addr:#x} size={size:#x} '
                      f'offset={s_off:#x} flags={s_flags:#x}')
                text = (addr, size, s_off)
                s += 80
        elif cmd == 0x5:
            flavor, count = struct.unpack_from('<II', b, off + 8)
            state = struct.unpack_from('<' + 'Q' * 34, b, off + 16)
            pc = state[32]
            print(f'        flavor={flavor} count={count} pc={pc:#x}')
            check(flavor == 6, 'flavor = ARM_THREAD_STATE64(6)')
            check(count == 68, 'count = 68')
        off += cmdsize

    check(text is not None, '找到 __TEXT,__text 节')
    check(pc is not None, '找到 LC_UNIXTHREAD 中的入口 PC')
    if text and pc is not None:
        addr, size, s_off = text
        check(addr <= pc < addr + size, f'入口 {pc:#x} 落在 __text 内 [{addr:#x}, {addr+size:#x})')

        print('\n代码反汇编：')
        code = b[s_off:s_off + size]
        words = struct.unpack('<' + 'I' * (len(code) // 4), code[:len(code) // 4 * 4])
        targets = []
        for i, w in enumerate(words):
            here = pc + i * 4
            dis = decode(w, here)
            print(f'  {here:#x}: {w:08x}  {dis}')
            if dis.startswith('adr'):
                targets.append(int(dis.split(', ')[1], 16))
            if dis.startswith('b.') or dis.startswith('b '):
                targets.append(int(dis.split()[-1], 16))

        blob_end = s_off + size
        print('\n跳转/地址目标检查：')
        for t in targets:
            inside_code = pc <= t < pc + size
            # 目标若在代码之后，应当是数据（marker / devpath）
            in_file = 0 <= t - pc + s_off < len(b)
            print(f'  {t:#x}  → {"代码" if inside_code else "数据区" if in_file else "越界"}')
            if not (inside_code or in_file):
                ok = False

    print('\n字符串检查：')
    for needle in (b'R11_EL0_INIT_OK', b'/dev/console'):
        found = needle in b
        print(f'  {"OK  " if found else "FAIL"} {needle!r}')
        if not found:
            ok = False

    print('\n结果:', '全部通过 ✅' if ok else '有问题 ❌')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
