#!/usr/bin/env python3
"""把引用了某个字符串的函数改成 `mov w0,#0; ret`（用于放行 AMFI/代码签名等检查）。

用法:
  python3 patch-kernel-fn.py <kernel.macho> --string "code signature validation failed" [--dry-run]

原理与 patch-apfs-roothash.py 相同：找字符串 VA → 在可执行段里找 adrp/add 交叉引用
→ 从引用处往回找最近的 ret，其后的第一条指令当作函数入口 → 写 mov w0,#0; ret。
幂等：入口已经是 mov w0,#0 就跳过。
"""
import struct
import sys

RET = 0xD65F03C0
MOV_W0_0 = 0x52800000


def segments(data):
    magic, cpu, sub, ftype, ncmds, sz, flags = struct.unpack_from('<IiiIIII', data, 0)
    off = 32
    segs = []
    for _ in range(ncmds):
        cmd, cmdsize = struct.unpack_from('<II', data, off)
        if cmd == 0x19:  # LC_SEGMENT_64
            name = data[off + 8:off + 24].split(b'\0')[0].decode('latin1')
            vmaddr, vmsize, fileoff, filesize = struct.unpack_from('<QQQQ', data, off + 24)
            _, initprot = struct.unpack_from('<ii', data, off + 56)
            segs.append(dict(name=name, vmaddr=vmaddr, vmsize=vmsize,
                             fileoff=fileoff, filesize=filesize, initprot=initprot))
        off += cmdsize
    return segs


def va_of_offset(segs, offset):
    for s in segs:
        if s['fileoff'] <= offset < s['fileoff'] + s['filesize']:
            return s['vmaddr'] + (offset - s['fileoff'])
    return None


def find_string(data, text):
    # 不要求后面跟 \0：AMFI 的日志串常常是长句的一部分（"... failed." 之类）。
    return data.find(text.encode())


def sign_extend(value, bits):
    if value & (1 << (bits - 1)):
        value -= 1 << bits
    return value


def adrp_target(pc, ins):
    if (ins & 0x9F000000) != 0x90000000:
        return None
    rd = ins & 0x1F
    immlo = (ins >> 29) & 0x3
    immhi = (ins >> 5) & 0x7FFFF
    imm = sign_extend((immhi << 2) | immlo, 21) << 12
    return rd, (pc & ~0xFFF) + imm


def is_prologue(ins):
    return ((ins & 0x7FC00000) == 0x29000000 or   # stp/ldp
            (ins & 0xFFC00000) == 0xA9800000 or
            (ins & 0x7F800000) == 0x52800000 or   # movz/movn/movk
            (ins & 0xFFE00000) == 0xD1000000 or   # sub sp
            (ins & 0xFFC00000) == 0xD1000000)


def patch_string(data, segs, text, dry):
    at = find_string(data, text)
    if at is None:
        print('  找不到字符串 %r' % text)
        return 0
    str_va = va_of_offset(segs, at)
    print('  字符串 %r @ VA 0x%x' % (text, str_va))
    hits = []
    for seg in segs:
        if not seg['filesize'] or not (seg['initprot'] & 0x4):
            continue
        base = seg['fileoff']
        for i in range(seg['filesize'] // 4 - 1):
            pc = seg['vmaddr'] + i * 4
            ins = struct.unpack_from('<I', data, base + i * 4)[0]
            pair = adrp_target(pc, ins)
            if not pair:
                continue
            rd, page = pair
            add = struct.unpack_from('<I', data, base + (i + 1) * 4)[0]
            if (add & 0x9F000000) != 0x91000000:
                continue
            if (add & 0x1F) != rd or ((add >> 5) & 0x1F) != rd:
                continue
            if page + ((add >> 10) & 0xFFF) == str_va:
                hits.append((pc, base + i * 4))
    print('  adrp/add 交叉引用: %d 处' % len(hits))
    n = 0
    for pc, fileoff in hits:
        back = 0
        while back < 0x800:
            cur = fileoff - back * 4
            if cur < 0:
                break
            if struct.unpack_from('<I', data, cur)[0] == RET:
                break
            back += 1
        else:
            print('    0x%x: 往回没找到 ret，跳过' % pc)
            continue
        ret_at = fileoff - back * 4
        if struct.unpack_from('<I', data, ret_at - 4)[0] == MOV_W0_0:
            print('    0x%x: 已打过（入口 0x%x）' % (pc, va_of_offset(segs, ret_at - 4)))
            n += 1
            continue
        start = ret_at + 4
        first = struct.unpack_from('<I', data, start)[0]
        start_va = va_of_offset(segs, start)
        if not is_prologue(first):
            print('    0x%x: ret 之后不是序言（0x%08x），跳过' % (pc, first))
            continue
        print('    引用 0x%x → 函数入口 0x%x（0x%08x）' % (pc, start_va, first))
        if not dry:
            struct.pack_into('<I', data, start, MOV_W0_0)
            struct.pack_into('<I', data, start + 4, RET)
        n += 1
    return n


def main(argv):
    if len(argv) < 2:
        raise SystemExit(__doc__)
    path = argv[1]
    dry = '--dry-run' in argv
    strings = [argv[i + 1] for i, a in enumerate(argv) if a == '--string']
    if not strings:
        raise SystemExit('需要 --string')
    data = bytearray(open(path, 'rb').read())
    segs = segments(data)
    total = 0
    for s in strings:
        print('== %s' % s)
        total += patch_string(data, segs, s, dry)
    if dry:
        print('dry-run：未写盘（命中 %d）' % total)
        return 0
    open(path, 'wb').write(data)
    print('已写盘，命中 %d 处' % total)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
