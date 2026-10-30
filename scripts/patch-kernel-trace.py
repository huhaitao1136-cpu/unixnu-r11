#!/usr/bin/env python3
"""在 kernel.macho 里装三个探针，用来判定"PID 1 到底有没有进 EL0"。

为什么要它：v5/v6 的 init（kill(1,SIGKILL) 都放了）在真机上一行输出都没有，
而 `kern_sig.c` 对 PID 1 的 SIGKILL 是 `panic_plain("unexpected SIGKILL ...")`
—— 没 panic 就等于用户态一条指令都没跑。所以要在内核侧打点。

探针：
  A  `_thread_bootstrap_return`       打印 `R11: bootstrap_return()`
  B  `_arm64_thread_exception_return` 打印 `R11: to-user elr=.. spsr=..`
     （xnu 回到用户态的唯一出口；spsr 的 M[3:2] 为 0 才是 EL0）
  C  `_unix_syscall`                  打印 `R11: svc <x16>`（EL0 发起的系统调用）

判读：
  A 出现但 B 没有            → 卡在 bootstrap 里没到 return_to_user
  B 出现且 spsr=EL0、elr 是用户地址，但 C 从不出现 → 进了 EL0 但没发出任何 svc
  C 出现                     → EL0 真的在跑

跳板要点：被钩函数的第一条指令必须**重放**（`_arm64_thread_exception_return`
的第一条是 `mrs x0, tpidr_el1`，`_unix_syscall` 的第一条是 `sub sp,sp,#0x70`，
跳过就会把栈搞坏）。所以跳板尾部按原指令类型分别处理：
  `b T`  → 直接 `b T`
  `bl T` → `bl T` 后 `b hook+4`
  其它   → 原样重放后 `b hook+4`

用法：
  python3 patch-kernel-trace.py <kernel> [<output>] --symbols <kernel-symbols.txt>
"""

import struct
import sys
from pathlib import Path

MAGIC = b'R11TRACE'
CAVE_SIZE = 4096
PREFER = ('__TEXT_EXEC', '__TEXT', '__PLK_TEXT_EXEC')

SYM_KPRINTF = '_kprintf'

SLOT = 60 * 4            # 每个跳板预留 60 条指令

# 探针表。`lip`/`lipexit`/`exec`/`machfile`/`act`/`pagein` 用来把
# `load_init_program` 内部切成几段，定位 exec 到底卡在哪一步；
# `svc` 是"EL0 真的跑起来了"的硬证据。
PROBES = {
    'lip': ('_load_init_program', 'plain', b'R11: LIP enter\n\0'),
    'boot': ('_thread_bootstrap_return', 'plain', b'R11: bootstrap_return()\n\0'),
    'ret': ('_thread_exception_return', 'elr', b'R11: ret-to elr=%08lx spsr=%08lx\n\0'),
    'to': ('_arm64_thread_exception_return', 'elr', b'R11: to-user elr=%08lx spsr=%08lx\n\0'),
    'eret': ('return_to_user', 'elr', b'R11: ERET elr=%08lx spsr=%08lx\n\0'),
    'lipexit': ('_load_init_program', 'lipexit', b'R11: LIP exit elr=%08lx spsr=%08lx\n\0'),
    'exec': ('_exec_mach_imgact', 'plain', b'R11: exec_mach_imgact enter\n\0'),
    'machfile': ('_load_machfile', 'plain', b'R11: load_machfile enter\n\0'),
    'act': ('_activate_exec_state', 'plain', b'R11: activate_exec_state enter\n\0'),
    'pagein': ('_vnode_pagein', 'plain', b'R11: vnode_pagein\n\0'),
    'mfileexit': ('_load_machfile', 'lipexit', b'R11: load_machfile exit ret=%lx elr=%08lx spsr=%08lx\n\0'),
    'getattr': ('_vnode_getattr', 'plain', b'R11: vnode_getattr\n\0'),
    'ubc': ('_ubc_getsize', 'plain', b'R11: ubc_getsize\n\0'),
    'upl': ('_ubc_create_upl', 'plain', b'R11: ubc_create_upl\n\0'),
    'mmap': ('_VNOP_MMAP', 'plain', b'R11: VNOP_MMAP\n\0'),
    'extract': ('_exec_extract_strings', 'plain', b'R11: exec_extract_strings\n\0'),
    'sugid': ('_exec_handle_sugid', 'plain', b'R11: exec_handle_sugid\n\0'),
    'stack': ('_create_unix_stack', 'plain', b'R11: create_unix_stack\n\0'),
    'copyout': ('_exec_copyout_strings', 'plain', b'R11: exec_copyout_strings\n\0'),
    'zre': ('_zone_userspace_reboot_checks', 'plain', b'R11: zone_userspace_reboot_checks\n\0'),
    'plat': ('_proc_setplatformdata', 'plain', b'R11: proc_setplatformdata\n\0'),
    'ovm': ('_vm_map_set_size_limit', 'plain', b'R11: vm_map_set_size_limit\n\0'),
    'svc': ('_unix_syscall', 'svc', b'R11: svc=%lu\n\0'),
}
DEFAULT_PROBES = ('lip,exec,machfile,mfileexit,extract,zre,plat,ovm,'
                  'act,lipexit,svc')


def select_probes(spec):
    if spec.strip() == 'all':
        names = list(PROBES)
    else:
        names = [n.strip() for n in spec.split(',') if n.strip()]
    out = []
    for name in names:
        if name not in PROBES:
            raise SystemExit(f'!! 未知探针 {name}（可选 {", ".join(PROBES)} 或 all）')
        out.append(PROBES[name])
    return out


# ---------- Mach-O ----------

def parse_segments(data):
    magic, _, _, filetype, ncmds, _, _, _ = struct.unpack_from('<IIIIIIII', data, 0)
    if magic not in (0xFEEDFACF, 0xFEEDFACE):
        raise SystemExit('!! 不是 Mach-O')
    off = 32
    segs = []
    for _ in range(ncmds):
        cmd, cmdsize = struct.unpack_from('<II', data, off)
        if cmd == 0x19:
            name = data[off + 8: off + 24].rstrip(b'\0').decode('ascii', 'replace')
            vmaddr, vmsize, fileoff, filesize, maxprot, initprot = \
                struct.unpack_from('<QQQQII', data, off + 24)
            segs.append(dict(name=name, vmaddr=vmaddr, vmsize=vmsize,
                             fileoff=fileoff, filesize=filesize,
                             maxprot=maxprot, initprot=initprot))
        off += cmdsize
    return segs, filetype


def va_to_off(segs, va):
    for s in segs:
        if s['vmaddr'] <= va < s['vmaddr'] + s['filesize']:
            return s['fileoff'] + (va - s['vmaddr'])
    return None


def cave_in_segment(data, s, need):
    base = s['fileoff']
    blob = data[base: base + s['filesize']]
    best = None
    run_start = None
    for i, byte in enumerate(blob):
        if byte == 0:
            if run_start is None:
                run_start = i
        elif run_start is not None:
            length = i - run_start
            if length >= need and (best is None or length > best[2]):
                best = (s['vmaddr'] + run_start, base + run_start, length)
            run_start = None
    if run_start is not None:
        length = len(blob) - run_start
        if length >= need and (best is None or length > best[2]):
            best = (s['vmaddr'] + run_start, base + run_start, length)
    return best


def find_cave(data, segs, need):
    """优先 __TEXT_EXEC：两个钩子就在这段里，确定被映射，而且 gc_enable 的
    跳板已经在这段里被真机验证过；__PLK_TEXT_EXEC 里虽然有大片零区，放最后。"""
    for want in PREFER:
        for s in segs:
            if s['name'] != want or not (s['initprot'] & 0x4) or s['filesize'] < need:
                continue
            hit = cave_in_segment(data, s, need)
            if hit:
                return hit
    return None


# ---------- 指令编码 ----------

def enc_b(pc, target):
    delta = target - pc
    if delta % 4 or not (-(1 << 27) <= delta < (1 << 27)):
        raise SystemExit(f'!! B 编码失败 {pc:#x} -> {target:#x}')
    return 0x14000000 | ((delta >> 2) & 0x03FFFFFF)


def enc_bl(pc, target):
    delta = target - pc
    if delta % 4 or not (-(1 << 27) <= delta < (1 << 27)):
        raise SystemExit(f'!! BL 编码失败 {pc:#x} -> {target:#x}')
    return 0x94000000 | ((delta >> 2) & 0x03FFFFFF)


def enc_adrp_x0(pc, target):
    page_delta = ((target & ~0xFFF) - (pc & ~0xFFF)) >> 12
    if not (-(1 << 20) <= page_delta < (1 << 20)):
        raise SystemExit(f'!! ADRP 超出 ±4GB {pc:#x} -> {target:#x}')
    immlo = page_delta & 0x3
    immhi = (page_delta >> 2) & 0x7FFFF
    return 0x90000000 | (immlo << 29) | (immhi << 5)


def enc_add_x0_imm(imm12):
    return 0x91000000 | ((imm12 & 0xFFF) << 10)


def enc_stp_pre(rt, rt2, rn, offset):
    imm7 = (offset // 8) & 0x7F
    return 0xA9800000 | (imm7 << 15) | (rt2 << 10) | (rn << 5) | rt


def enc_ldp_post(rt, rt2, rn, offset):
    imm7 = (offset // 8) & 0x7F
    return 0xA8C00000 | (imm7 << 15) | (rt2 << 10) | (rn << 5) | rt


def enc_mov_xd_xs(rd, rm):
    return 0xAA0003E0 | (rm << 16) | rd


ENC_MRS_ELR_EL1 = 0xD5384021     # mrs x1, elr_el1
ENC_MRS_SPSR_EL1 = 0xD5384002    # mrs x2, spsr_el1


# ---------- 符号表 ----------

def parse_symbols(path):
    table = {}
    for line in Path(path).read_text(encoding='utf-8', errors='replace').splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        try:
            addr = int(parts[0], 16)
        except ValueError:
            continue
        for name in (parts[2], parts[2].lstrip('_')):
            table.setdefault(name, addr)
    return table


def sym(table, name):
    for key in (name, '_' + name.lstrip('_')):
        if key in table:
            return table[key]
    raise SystemExit(f'!! 找不到符号 {name}')


# ---------- 跳板 ----------

def replay_tail(orig, pc, hook):
    """把被钩函数的第一条指令补回来。"""
    if (orig & 0xFC000000) == 0x14000000:                    # b
        imm = orig & 0x03FFFFFF
        if imm & (1 << 25):
            imm -= 1 << 26
        return [enc_b(pc, pc + imm * 4)]
    if (orig & 0xFC000000) == 0x94000000:                    # bl
        imm = orig & 0x03FFFFFF
        if imm & (1 << 25):
            imm -= 1 << 26
        return [enc_bl(pc, pc + imm * 4), enc_b(pc + 4, hook + 4)]
    return [orig, enc_b(pc + 4, hook + 4)]


def build_tramp(base, hook, kprintf, kind, fmt_va, orig, tail_words=None):
    words = []
    pc = base
    words.append(enc_stp_pre(29, 30, 31, -16)); pc += 4
    words.append(enc_stp_pre(0, 1, 31, -16)); pc += 4
    words.append(enc_stp_pre(2, 3, 31, -16)); pc += 4
    if kind == 'lipexit':
        # 打印返回值 + ELR/SPSR。注意：ENC_MRS_ELR_EL1 写的是 x1、ENC_MRS_SPSR_EL1 写的是 x2，
        # 所以先把返回值挪到 x4，读完之后重排成 kprintf(fmt, ret, elr, spsr) = (x1, x2, x3)。
        words.append(enc_mov_xd_xs(4, 0)); pc += 4      # x4 = w0（返回值）
        words.append(ENC_MRS_ELR_EL1); pc += 4          # x1 = elr
        words.append(ENC_MRS_SPSR_EL1); pc += 4         # x2 = spsr
        words.append(enc_mov_xd_xs(3, 2)); pc += 4      # x3 = spsr
        words.append(enc_mov_xd_xs(2, 1)); pc += 4      # x2 = elr
        words.append(enc_mov_xd_xs(1, 4)); pc += 4      # x1 = ret
    elif kind == 'elr':
        words.append(ENC_MRS_ELR_EL1); pc += 4
        words.append(ENC_MRS_SPSR_EL1); pc += 4
    elif kind == 'svc':
        words.append(enc_mov_xd_xs(1, 16)); pc += 4
    words.append(enc_adrp_x0(pc, fmt_va)); pc += 4
    words.append(enc_add_x0_imm(fmt_va & 0xFFF)); pc += 4
    words.append(enc_bl(pc, kprintf)); pc += 4
    words.append(enc_ldp_post(2, 3, 31, 16)); pc += 4
    words.append(enc_ldp_post(0, 1, 31, 16)); pc += 4
    words.append(enc_ldp_post(29, 30, 31, 16)); pc += 4
    tail = tail_words if tail_words is not None else replay_tail(orig, pc, hook)
    for w in tail:
        words.append(w); pc += 4
    return b''.join(struct.pack('<I', w) for w in words)


def last_ret_in_function(data, segs, table, func_va):
    """函数范围 [func_va, 下一个符号)，返回最后一个 `ret` 的地址。"""
    addrs = sorted(set(v for v in table.values() if v > func_va))
    end = addrs[0] if addrs else func_va + 0x4000
    lo = va_to_off(segs, func_va)
    if lo is None:
        raise SystemExit(f'!! {func_va:#x} 不在文件里')
    count = (end - func_va) // 4
    hit = None
    for k in range(count):
        w = struct.unpack_from('<I', data, lo + 4 * k)[0]
        if w == 0xD65F03C0:
            hit = func_va + 4 * k
    if hit is None:
        raise SystemExit(f'!! {func_va:#x}..{end:#x} 里没有 ret')
    return hit


def main():
    argv = sys.argv[1:]
    symbols_path = None
    probes_spec = DEFAULT_PROBES
    positional = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ('--symbols', '--probes') and i + 1 < len(argv):
            value = argv[i + 1]
            if a == '--symbols':
                symbols_path = value
            else:
                probes_spec = value
            i += 2
            continue
        if a.startswith('--symbols='):
            symbols_path = a.split('=', 1)[1]
        elif a.startswith('--probes='):
            probes_spec = a.split('=', 1)[1]
        elif not a.startswith('--'):
            positional.append(a)
        i += 1
    if not positional or not symbols_path:
        print(__doc__)
        return 2
    src = Path(positional[0])
    dst = Path(positional[1]) if len(positional) > 1 else src
    data = bytearray(src.read_bytes())
    segs, _ = parse_segments(data)

    if MAGIC in data:
        print('  已经装过 R11TRACE 探针，跳过')
        if dst != src:
            dst.write_bytes(data)
        return 0

    table = parse_symbols(symbols_path)
    kprintf = sym(table, SYM_KPRINTF)
    hooks = []
    for name, kind, msg in select_probes(probes_spec):
        va = sym(table, name)
        hook_va = va
        tail = None
        if kind == 'lipexit':
            # 钩函数的最后一个 ret，而不是入口：exec 已经做完，
            # 此时 ELR_EL1/SPSR_EL1 里就是"即将回到用户态的状态"。
            hook_va = last_ret_in_function(data, segs, table, va)
            tail = [0xD65F03C0]
        hooks.append(dict(name=name, kind=kind, msg=msg, va=hook_va, func=va, tail=tail))
    print(f'  探针集合: {probes_spec}')
    print('  符号: ' + ' '.join(f'{h["name"]}={h["func"]:#x}' for h in hooks)
          + f' {SYM_KPRINTF}={kprintf:#x}')

    cave = find_cave(data, segs, CAVE_SIZE)
    if not cave:
        raise SystemExit(f'!! 找不到 {CAVE_SIZE} 字节的全零空穴')
    cave_va, cave_off, cave_len = cave
    print(f'  空洞: {cave_va:#x}（{cave_len} 字节可用，取 {CAVE_SIZE}）')

    # 布局：魔数(8) → 每个钩子一个 SLOT → 三个格式串
    cursor = cave_va + len(MAGIC)
    tramps = []
    for h in hooks:
        h['tramp_va'] = cursor
        cursor += SLOT
    cursor = (cursor + 7) & ~7
    for h in hooks:
        h['fmt_va'] = cursor
        cursor += len(h['msg'])
    if cursor - cave_va > cave_len:
        raise SystemExit('!! 空穴装不下探针')

    for h in hooks:
        hoff = va_to_off(segs, h['va'])
        if hoff is None:
            raise SystemExit(f'!! {h["name"]} 不在可写的文件区间里')
        h['orig'] = struct.unpack_from('<I', data, hoff)[0]
        h['tramp'] = build_tramp(h['tramp_va'], h['va'], kprintf, h['kind'],
                                 h['fmt_va'], h['orig'], h['tail'])

    blob = bytearray(MAGIC)
    for h in hooks:
        want = va_to_off(segs, h['tramp_va'])
        here = cave_off + len(blob)
        if want > here:
            blob += b'\0' * (want - here)
        blob += h['tramp']
    for h in hooks:
        want = va_to_off(segs, h['fmt_va'])
        here = cave_off + len(blob)
        if want > here:
            blob += b'\0' * (want - here)
        blob += h['msg']
    if len(blob) > CAVE_SIZE:
        raise SystemExit('!! 探针 blob 超出空穴')
    data[cave_off: cave_off + len(blob)] = blob

    for h in hooks:
        hoff = va_to_off(segs, h['va'])
        struct.pack_into('<I', data, hoff, enc_b(h['va'], h['tramp_va']))
        print(f'  {h["name"]} {h["va"]:#x}: {h["orig"]:08x} -> b {h["tramp_va"]:#x}')

    dst.write_bytes(data)
    print(f'  输出: {dst}')
    print('  探针: ' + ' / '.join(f'{h["kind"]}@{h["tramp_va"]:#x}(msg {h["fmt_va"]:#x})'
                                 for h in hooks))
    return 0


if __name__ == '__main__':
    sys.exit(main())
