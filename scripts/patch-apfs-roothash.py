#!/usr/bin/env python3
"""把 APFS 的 `is_root_hash_authentication_required()` 打成「永远返回 0」。

背景：种子卷被 APFS 归类为 `Root Volume`，于是 `apfs_vfsop_mountroot` 之后走到
`is_root_hash_authentication_required:555: disk1s1 Root Volume, root hash authentication is required`
就停住了（我们既没有 iBoot 注入的 root hash，也没有 AMFI 认证器）。这个函数只是"要不要
校验卷的 root hash"的开关，返回 0 即跳过校验。

内核符号表里没有 APFS kext 的内部符号（prelink 后 `lc_symtab` 只有 7 万个主内核符号），
所以按 **字符串 `is_root_hash_authentication_required` 的 adrp/add 交叉引用** 定位函数，
再在函数入口写 `mov w0,#0 ; ret`。

用法：
    python3 patch-apfs-roothash.py <kernel.macho> [--dry-run]
"""

import struct
import sys

def is_prologue(ins):
    """函数入口常见的两条：`stp xN, xM, [sp, #-imm]!`（pre-index）与 `sub sp, sp, #imm`。"""
    if (ins & 0xFFC00000) == 0xA9800000:   # stp (pre-index, 64 位)
        return True
    if (ins & 0xFF8003FF) == 0xD10003FF:   # sub sp, sp, #imm
        return True
    return False


RET = 0xD65F03C0
MOV_W0_0 = 0x52800000


def segments(data):
    magic, cputype, cpusub, filetype, ncmds, sizeofcmds, flags, _ = struct.unpack_from('<IiiIIIII', data, 0)
    if magic != 0xFEEDFACF:
        raise SystemExit('不是 64 位 Mach-O: %#x' % magic)
    off = 32
    out = []
    for _ in range(ncmds):
        cmd, cmdsize = struct.unpack_from('<II', data, off)
        if cmd == 0x19:  # LC_SEGMENT_64
            (segname, vmaddr, vmsize, fileoff, filesize,
             maxprot, initprot, nsects, segflags) = struct.unpack_from('<16sQQQQiiII', data, off + 8)
            out.append(dict(name=segname.rstrip(b'\x00').decode(),
                            vmaddr=vmaddr, vmsize=vmsize,
                            fileoff=fileoff, filesize=filesize,
                            initprot=initprot))
        off += cmdsize
    return out


def va_of_offset(segs, offset):
    for seg in segs:
        if seg['filesize'] and seg['fileoff'] <= offset < seg['fileoff'] + seg['filesize']:
            return seg['vmaddr'] + (offset - seg['fileoff'])
    return None


def offset_of_va(segs, va):
    for seg in segs:
        if seg['vmsize'] and seg['vmaddr'] <= va < seg['vmaddr'] + seg['vmsize']:
            if seg['filesize'] and seg['vmaddr'] + seg['filesize'] > va:
                return seg['fileoff'] + (va - seg['vmaddr'])
    return None


def find_string(data, text):
    blob = text.encode()
    needle = b'\x00' + blob + b'\x00'
    idx = data.find(needle)
    if idx < 0:
        idx = data.find(blob)
        if idx < 0:
            return None
        return idx
    return idx + 1


def sign_extend(value, bits):
    if value & (1 << (bits - 1)):
        return value - (1 << bits)
    return value


def adrp_target(pc, ins):
    # 注意：ADRP 的掩码只到 0x9F000000（Rd 在低 5 位，不能算进掩码里）
    if (ins & 0x9F000000) != 0x90000000:
        return None
    rd = ins & 0x1F
    immlo = (ins >> 29) & 0x3
    immhi = (ins >> 5) & 0x7FFFF
    page = (pc & ~0xFFF) + (sign_extend((immhi << 2) | immlo, 21) << 12)
    return rd, page & 0xFFFFFFFFFFFFFFFF


def main(argv):
    if len(argv) < 2:
        raise SystemExit(__doc__)
    path = argv[1]
    dry = '--dry-run' in argv
    data = bytearray(open(path, 'rb').read())
    segs = segments(data)

    at = find_string(data, 'is_root_hash_authentication_required')
    if at is None:
        raise SystemExit('找不到字符串 is_root_hash_authentication_required')
    str_va = va_of_offset(segs, at)
    print('字符串 @ 文件 0x%x / VA 0x%x' % (at, str_va))

    hits = []
    for seg in segs:
        if not seg['filesize'] or not (seg['initprot'] & 0x4):  # 需要可执行段
            continue
        base = seg['fileoff']
        count = seg['filesize'] // 4
        for i in range(count - 1):
            pc = seg['vmaddr'] + i * 4
            ins = struct.unpack_from('<I', data, base + i * 4)[0]
            pair = adrp_target(pc, ins)
            if not pair:
                continue
            rd, page = pair
            add = struct.unpack_from('<I', data, base + (i + 1) * 4)[0]
            # ADD (immediate), 64 位：掩码同样只能到 opcode 位（Rd/Rn 都在低 10 位里）
            if (add & 0x9F000000) != 0x91000000:
                continue
            if (add & 0x1F) != rd or ((add >> 5) & 0x1F) != rd:
                continue
            imm12 = (add >> 10) & 0xFFF
            if page + imm12 == str_va:
                hits.append((seg['name'], pc, base + i * 4))

    print('adrp/add 交叉引用: %d 处' % len(hits))
    if not hits:
        raise SystemExit('没找到引用，放弃')

    patched = 0
    for name, pc, fileoff in hits:
        # 函数入口 = 最近一个 ret 之后的**第一条**指令；不再向前漂移
        # （第一版会一路向前找 prologue，打完补丁后入口变成 mov/ret，第二次跑就会
        #  漂到函数体内的 `sub sp, sp, #0x430` 上，把函数体改坏）
        back = 0
        while back < 0x600:
            cur = fileoff - back * 4
            if cur < 0:
                break
            if struct.unpack_from('<I', data, cur)[0] == RET:
                break
            back += 1
        else:
            print('  %s 0x%x: 往回 0x600 字节内没找到 ret，跳过' % (name, pc))
            continue
        ret_at = fileoff - back * 4
        # 已经打过补丁的情形：最近那个 ret 的前一条就是 `mov w0,#0`（我们写进去的 mov/ret 对）
        if struct.unpack_from('<I', data, ret_at - 4)[0] == MOV_W0_0:
            where = va_of_offset(segs, ret_at - 4)
            print('  %s 0x%x: 已经打过补丁（0x%x = mov w0,#0; ret）' % (name, where, where))
            patched += 1
            continue
        start = ret_at + 4
        first = struct.unpack_from('<I', data, start)[0]
        start_va = va_of_offset(segs, start)
        if not is_prologue(first):
            print('  %s 0x%x: ret 之后不是函数序言（0x%08x），跳过' % (name, start_va, first))
            continue
        print('  %s 引用 0x%x → 函数入口 0x%x（入口指令 0x%08x）' % (name, pc, start_va, first))
        if not dry:
            struct.pack_into('<I', data, start, MOV_W0_0)
            struct.pack_into('<I', data, start + 4, RET)
        patched += 1

    if dry:
        print('dry-run：未写盘')
        return 0
    if patched == 0:
        raise SystemExit('一处都没打上，放弃')
    open(path, 'wb').write(bytes(data))
    print('已写回 %s（%d 处）' % (path, patched))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
