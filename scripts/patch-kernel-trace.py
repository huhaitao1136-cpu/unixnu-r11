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

import hashlib
import struct
import sys
from pathlib import Path

MAGIC = b'R11TRACE'
CAVE_SIZE = 4096
PREFER = ('__TEXT_EXEC', '__TEXT', '__PLK_TEXT_EXEC')

SYM_KPRINTF = '_kprintf'

SLOT = 60 * 4            # 每个跳板预留 60 条指令

# `reason` 探针一行只打一个值，三个格式串分别落在 msg+0 / +0x40 / +0x80，
# 所以这段缓冲必须按 0x40 对齐填满（纯单行三参数那版把值打串过位置）。
_REASON_LINES = (b'R11: reason ns=%x\n\0', b'R11: reason code=%x\n\0', b'R11: reason lr=%x\n\0')
REASON_MSG = b''.join(s + b'\0' * (0x40 - len(s)) for s in _REASON_LINES) + b'\0' * 8
assert REASON_MSG[0x40:0x41] == b'R'[:1] and REASON_MSG[0x80:0x81] == b'R'[:1]

# 探针表。`lip`/`lipexit`/`exec`/`machfile`/`act`/`pagein` 用来把
# `load_init_program` 内部切成几段，定位 exec 到底卡在哪一步；
# `svc` 是"EL0 真的跑起来了"的硬证据。
PROBES = {    'lip': ('_load_init_program', 'plain', b'R11: LIP enter\n\0'),
    'amfir': ('_amfi_interface_register', 'ptrs3',
              b'R11: amfi ops=%x f0=%x f1=%x\n\0'),
    'boot': ('_thread_bootstrap_return', 'plain', b'R11: bootstrap_return()\n\0'),
    'ret': ('_thread_exception_return', 'elr', b'R11: ret-to elr=%08lx spsr=%08lx\n\0'),
    'to': ('_arm64_thread_exception_return', 'elr', b'R11: to-user elr=%08lx spsr=%08lx\n\0'),
    'eret': ('return_to_user', 'elr', b'R11: ERET elr=%08lx spsr=%08lx\n\0'),
    'lipexit': ('_load_init_program', 'lipexit', b'R11: LIP exit elr=%08lx spsr=%08lx\n\0'),
    'exec': ('_exec_mach_imgact', 'plain', b'R11: exec_mach_imgact enter\n\0'),
    'machfile': ('_load_machfile', 'plain', b'R11: load_machfile enter\n\0'),
    'act': ('_activate_exec_state', 'plain', b'R11: activate_exec_state enter\n\0'),
    'pagein': ('_vnode_pagein', 'plain', b'R11: vnode_pagein\n\0'),
    'mfileexit': ('_load_machfile', 'lipexit', b'R11: load_machfile exit ret=%x\n\0'),
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
    'lerr': ('_load_return_to_errno', 'arg0', b'R11: load_return_to_errno lret=%x\n\0'),
    'lret0': ('0xfffffff0076d4b5c', 'arg0', b'R11: [caller] w0=%x\n\0'),
    # parse_machfile 的统一收尾：0x7762c78 `mov x0, x19` → ret。
    # 成功和失败都从这里出去，打 x19 就是它的返回值。
    'pmret': ('0xfffffff007762c78', 'reg19', b'R11: parse_machfile -> x19=%x\n\0'),
    'err19': ('0xfffffff007763aec', 'err19', b'R11: err19 w19=%x w1=%x w3=%x\n\0'),
    # 全内核 43 个 os_reason_create 调用点，只有把返回地址也打出来才能认出是谁在报错。
    'reason': ('_os_reason_create', 'ns_code_lr', REASON_MSG),
    # 不要赌函数入口的寄存器意义，直接钉在 parse_machfile 自己的调用点上：
    # 0x7763b2c `mov x1, x19` / 0x7763b30 `bl _os_reason_create`，
    # 所以 bl 这一条上的 x1 就是它要上报的 reason code，x0 是 namespace。
    'pcode': ('0xfffffff007763b30', 'arg1', b'R11: parse reason code=%x\n\0'),
    # 反汇编证明：_load_machfile 的失败返回值就是 parse_machfile 的返回值，
    # 而后者在 0x7763b34 被 os_reason_create 的返回值覆盖 → 真错误码只在
    # 0x7763b2c `mov x1, x19` 这一瞬还活着。挂这里读 w19 是唯一干净的办法。
    'rcode': ('0xfffffff007763b2c', 'reg19', b'R11: real lret=%x\n\0'),
    'pmach': ('_parse_machfile', 'plain', b'R11: parse_machfile enter\n\0'),
    # parse_machfile 成功后，load_machfile 里下一道硬关：代码签名。
    'sig': ('_load_code_signature', 'plain', b'R11: load_code_signature enter\n\0'),
    # 单值版：只打 x1（reason code）。三个值一起搬那版真机打出三行同一个数，
    # 说明「搬参数」这一步本身不可信；这里不做任何 shuffle，x1 原样打。
    'reasonc': ('_os_reason_create', 'arg1', b'R11: reason code=%x\n\0'),
    # parse/签名都过了 AMFI 才拦 → 说明我们的 init 是在某个 MACF 回调里被拒的。
    'psig': ('_process_signature', 'plain', b'R11: process_signature enter\n\0'),
    'psigr': ('_process_signature', 'lipexit', b'R11: process_signature exit ret=%x\n\0'),
    # exec_mach_imgact 里 9 条 bl _os_reason_create 的调用点（反汇编实测），
    # 每条传的 code 都是常量：0xc / 2 / 3 / 4 / 5 / 6 / 7 / 0x11。
    # 打这些点就能知道"exec 自己有没有报错、报的是哪一条"，
    # 不用再猜 reason 参数（实测那路打出来是野值）。
    'x9c': ('0xfffffff0076d4b98', 'plain', b'R11: exec reason 0xc\n\0'),
    'x9a': ('0xfffffff0076d4e3c', 'plain', b'R11: exec reason 2\n\0'),
    'x9b': ('0xfffffff0076d4ed8', 'plain', b'R11: exec reason 3\n\0'),
    'x9d': ('0xfffffff0076d4f6c', 'plain', b'R11: exec reason 4\n\0'),
    'x9e': ('0xfffffff0076d5098', 'plain', b'R11: exec reason 5\n\0'),
    'x9f': ('0xfffffff0076d5120', 'plain', b'R11: exec reason 6\n\0'),
    'x9g': ('0xfffffff0076d51d0', 'plain', b'R11: exec reason 7\n\0'),
    'x9h': ('0xfffffff0076d5244', 'plain', b'R11: exec reason 0x11\n\0'),
    'x9i': ('0xfffffff0076d54ac', 'plain', b'R11: exec reason tail\n\0'),
    # parse_machfile 自己只有三处返回小整数：BADARCH(1)@0x7762c74、BADMACHO(4)@0x7762c38、
    # 以及 0x7763b34 那条被 reason 指针覆盖的路径。三个点一起打就能分清是哪一类。
    'bad1': ('0xfffffff007762c74', 'plain', b'R11: parse -> LOAD_BADARCH(1)\n\0'),
    'bad4': ('0xfffffff007762c38', 'plain', b'R11: parse -> LOAD_BADMACHO(4)\n\0'),
    # 只靠常量打印的"进度探针"：不读任何寄存器，能可靠回答"parse 走到哪一步"。
    # 顺序应为 load_segment → load_unixthread（正常 Mach-O 都会经过）。
    'seg': ('_load_segment', 'plain', b'R11: load_segment enter\n\0'),
    'unix': ('_load_unixthread', 'plain', b'R11: load_unixthread enter\n\0'),
    # 直接读 _load_unixthread 自己的返回值（小整数枚举），不经过任何包装。
    'unixr': ('_load_unixthread', 'lipexit', b'R11: load_unixthread ret=%x\n\0'),
    # 段映射阶段的打点：__PAGEZERO 是 4 GiB，vme 不出现就说明卡在映射之前。
    'vme': ('_vm_map_enter', 'plain', b'R11: vm_map_enter\n\0'),
    # load_machfile 的最后一个 ret：打它自己的返回值（lipexit 打 w0）。
    'mexit': ('_load_machfile', 'lipexit', b'R11: load_machfile exit ret=%x\n\0'),
    'pmexit': ('_parse_machfile', 'lipexit', b'R11: parse_machfile exit ret=%x\n\0'),
    'mexit19': ('0xfffffff007762a6c', 'reg19', b'R11: load_machfile returns w19=%x\n\0'),
    'mfilefail': ('0xfffffff0076d4b64', 'reg24', b'R11: load_machfile FAILED lret=%x\n\0'),
    # _load_machfile 里紧跟 `bl _parse_machfile` 的那条 cbz：此刻 w0 还是
    # parse_machfile 的原始返回值，没被 mov x19,x0 / os_reason 覆盖。
    # 用它区分「parse 真的返回了错误码」还是「load 期间被覆盖」。
    'preret': ('0xfffffff007762a5c', 'arg0', b'R11: parse raw return=%x\n\0'),
    # parse_machfile 里 `bl _load_code_signature` 之后紧跟着 `cbz w0, 成功`，
    # 这条 cbz 上的 w0 就是签名加载的结果：没有输出＝根本没走到签名这一步。
    'sigret': ('0xfffffff007763494', 'arg0', b'R11: codesig ret=%x\n\0'),
    # parse_machfile 全函数只有 0xfffffff007762c98 这一个 ret；
    # 在 ret 之前打 w0，就能和调用方看到的 w0 逐值对照。
    'pret': ('0xfffffff007762c98', 'arg0', b'R11: parse ret=%x\n\0'),
    # parse_machfile 里给 x19 写小整数的错误出口：谁被打中就说明走到哪条判定。
    'e1': ('0xfffffff007762c74', 'plain', b'R11: branch LOAD_BADARCH(1)\n\0'),
    'e2': ('0xfffffff007762dd4', 'plain', b'R11: branch LOAD_BADMACHO(2)\n\0'),
    'e2b': ('0xfffffff007762e5c', 'plain', b'R11: branch LOAD_BADMACHO(2) b\n\0'),
    'e5': ('0xfffffff007762e4c', 'plain', b'R11: branch LOAD_NOSPACE(5)\n\0'),
    'e9': ('0xfffffff007762e44', 'plain', b'R11: branch LOAD_IOERROR(9)\n\0'),
    'e10': ('0xfffffff007763a44', 'plain', b'R11: branch LOAD_DECRYPTFAIL(10)\n\0'),
    'mfok': ('0xfffffff0076d4bb0', 'plain', b'R11: load_machfile OK\n\0'),
    'svc': ('_unix_syscall', 'svc', b'R11: svc=%lu\n\0'),
}
DEFAULT_PROBES = ('lip,exec,mfilefail,mfok,zre,sugid,act,lipexit,svc')

# 按字面地址挂的探针必须额外核对：地址对 + 该地址的原始指令对，
# 否则换内核后会静默地钩到别的指令上（历史教训，见 m07713）。
# 值 = (钩子地址, 该处原始指令)
EXPECT = {
    # `adrp x0, 0xfffffff007167000`，是共享错误出口里 csel 之后的第一条
    'err19': (0xfffffff007763aec, 0x90FFD020),
    # `cbz w0, 0xfffffff007762a90`，_load_machfile 里 bl _parse_machfile 之后
    'preret': (0xfffffff007762a5c, 0x340001A0),
    # `cbz w0, 0xfffffff007763724`，`bl _load_code_signature` 之后
    'sigret': (0xfffffff007763494, 0x34001480),
    # `ret`，parse_machfile 唯一返回点
    'pret': (0xfffffff007762c98, 0xD65F03C0),
    # 各处 `mov w19, #N`（N = load_return_t）
    'e1': (0xfffffff007762c74, 0x52800033),
    'e2': (0xfffffff007762dd4, 0x52800053),
    'e2b': (0xfffffff007762e5c, 0x52800053),
    'e5': (0xfffffff007762e4c, 0x528000B3),
    'e9': (0xfffffff007762e44, 0x52800133),
    'e10': (0xfffffff007763a44, 0x52800153),
}


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


def enc_adrp_xd(pc, target, rd):
    page_delta = ((target & ~0xFFF) - (pc & ~0xFFF)) >> 12
    if not (-(1 << 20) <= page_delta < (1 << 20)):
        raise SystemExit(f'!! ADRP 超出 ±4GB {pc:#x} -> {target:#x}')
    immlo = page_delta & 0x3
    immhi = (page_delta >> 2) & 0x7FFFF
    return 0x90000000 | (immlo << 29) | (immhi << 5) | (rd & 31)


def enc_adrp_x0(pc, target):
    return enc_adrp_xd(pc, target, 0)


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


def enc_ldr_xd_xn(rt, rn, offset):
    """ldr xt, [xn, #offset]（offset 必须是 8 的倍数）。"""
    imm12 = (offset // 8) & 0xFFF
    return 0xF9400000 | (imm12 << 10) | (rn << 5) | rt


def enc_ldr_xd_sp(rt, offset):
    """ldr xt, [sp, #offset]（offset 必须是 8 的倍数）。"""
    imm12 = (offset // 8) & 0xFFF
    return 0xF9400000 | (imm12 << 10) | (31 << 5) | rt


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
    if name.startswith('0x') or name.startswith('0X'):
        return int(name, 16)
    for key in (name, '_' + name.lstrip('_')):
        if key in table:
            return table[key]
    raise SystemExit(f'!! 找不到符号 {name}')


# ---------- 跳板 ----------

def replay_tail(orig, pc, hook):
    """把被钩函数的第一条指令补回来。

    坑：以前非跳转指令是**原样照搬**的。ADRP/ADR 是 PC 相对指令，
    挪到空穴里之后算出来的页就变了（实测 `adrp x8, 0x..7af1000` 被搬成
    `0x..7e4d000`，后面 `ldr w8` 读到另一个全局，分支全被带偏）。
    所以这两类必须按新地址重新编码。
    """
    if (orig & 0xFC000000) == 0x14000000:                    # b
        imm = orig & 0x03FFFFFF
        if imm & (1 << 25):
            imm -= 1 << 26
        # imm 是相对**原地址**的偏移，目标绝对地址必须先按 hook 算出来，
        # 不能直接拿空穴地址加偏移（那样会跳到别处）。
        return [enc_b(pc, hook + imm * 4)]
    if (orig & 0xFC000000) == 0x94000000:                    # bl
        imm = orig & 0x03FFFFFF
        if imm & (1 << 25):
            imm -= 1 << 26
        return [enc_bl(pc, hook + imm * 4), enc_b(pc + 4, hook + 4)]
    if (orig & 0x9F000000) == 0x90000000:                    # adrp
        rd = orig & 31
        immlo = (orig >> 29) & 0x3
        immhi = (orig >> 5) & 0x7FFFF
        page = (immlo | (immhi << 2))
        if page & (1 << 20):
            page -= 1 << 21
        target = ((hook + 4) & ~0xFFF) + (page << 12)
        return [enc_adrp_xd(pc, target, rd), enc_b(pc + 4, hook + 4)]
    return [orig, enc_b(pc + 4, hook + 4)]


def enc_mov_wd_ws(dst, src):
    """`mov wD, wS`（ORR wD, wzr, wS），顺带把高位清零。"""
    return 0x2A0003E0 | ((src & 31) << 16) | (dst & 31)


def build_tramp(base, hook, kprintf, kind, fmt_va, orig, tail_words=None):
    words = []
    pc = base
    words.append(enc_stp_pre(29, 30, 31, -16)); pc += 4
    words.append(enc_stp_pre(0, 1, 31, -16)); pc += 4
    words.append(enc_stp_pre(2, 3, 31, -16)); pc += 4
    if kind == 'lipexit':
        # 只打 32 位返回值：XNU kprintf 对 %lx 的 64 位宽度处理不对，会把高位截成乱码。
        words.append(enc_mov_wd_ws(1, 0)); pc += 4      # x1 = (uint32_t) w0
    elif kind == 'elr':
        words.append(ENC_MRS_ELR_EL1); pc += 4
        words.append(ENC_MRS_SPSR_EL1); pc += 4
    elif kind == 'reg24':
        words.append(enc_mov_wd_ws(1, 24)); pc += 4     # x1 = (uint32_t) w24
    elif kind == 'reg19':
        words.append(enc_mov_wd_ws(1, 19)); pc += 4     # x1 = (uint32_t) w19
    elif kind == 'arg0':
        words.append(enc_mov_wd_ws(1, 0)); pc += 4      # x1 = (uint32_t) 第一个参数
    elif kind == 'arg1':
        # 第二个参数本来就在 x1，前面只 stp 保存、没破坏它，直接打就行。
        pass
    elif kind == 'ns_code_lr':
        # 分三行各打一个值，每个值一个独立的格式串。
        # 原因：单行三参数那次打出 `ns=188638e0 code=a0 lr=9`，位置对不上，
        # 分三行能把「搬错参数」和「值本身就是怪」彻底区分开。
        # 栈布局：sp+0=(x2,x3) sp+16=(x0,x1) sp+32=(x29,x30)
        L = base + 4 * len(words)
        # 1) namespace = 原 x0
        L = base + 4 * len(words)
        words.append(enc_ldr_xd_sp(1, 16)); pc += 4
        words.append(enc_adrp_x0(L, fmt_va)); pc += 4
        words.append(enc_add_x0_imm(fmt_va & 0xFFF)); pc += 4
        words.append(enc_bl(pc, kprintf)); pc += 4
        # 2) code = 原 x1
        words.append(enc_ldr_xd_sp(1, 24)); pc += 4
        words.append(enc_adrp_x0(pc, fmt_va + 0x40)); pc += 4
        words.append(enc_add_x0_imm((fmt_va + 0x40) & 0xFFF)); pc += 4
        words.append(enc_bl(pc, kprintf)); pc += 4
        # 3) 返回地址 = 原 x30
        words.append(enc_ldr_xd_sp(1, 32)); pc += 4
        words.append(enc_adrp_x0(pc, fmt_va + 0x80)); pc += 4
        words.append(enc_add_x0_imm((fmt_va + 0x80) & 0xFFF)); pc += 4
        words.append(enc_bl(pc, kprintf)); pc += 4
        # 三个值都打完了，跳过下面统一的「一个格式串 + 一次 kprintf」路径
        words.append(enc_ldp_post(2, 3, 31, 16)); pc += 4
        words.append(enc_ldp_post(0, 1, 31, 16)); pc += 4
        words.append(enc_ldp_post(29, 30, 31, 16)); pc += 4
        for w in (tail_words if tail_words is not None else replay_tail(orig, pc, hook)):
            words.append(w); pc += 4
        return b''.join(struct.pack('<I', w) for w in words)
    elif kind == 'err19':
        # 打 (w19, w1, w3)。挂点 0xfffffff007763aec 紧跟在
        # `csel w19, w8, w1, eq` 之后、`mov x0,#9 / bl _os_reason_create` 之前：
        # 此时 w19 还是 csel 选出来的 load_return_t，没被 os_reason_t 覆盖。
        # w1/w3 一起打，才能看出 csel 到底选了哪一路（见 m07729 的判读表）。
        words.append(enc_mov_wd_ws(2, 1)); pc += 4      # x2 = (uint32_t) w1
        words.append(enc_mov_wd_ws(3, 3)); pc += 4      # x3 = (uint32_t) w3
        words.append(enc_mov_wd_ws(1, 19)); pc += 4     # x1 = (uint32_t) w19
    elif kind == 'ptrs3':
        # 打 (x0, [x0], [x0+8])：用于把注册进来的函数表地址和头两个函数指针
        # 直接变成屏幕上的数字，再拿数字去符号表里查，不靠猜。
        words.append(enc_mov_xd_xs(1, 0)); pc += 4
        words.append(enc_ldr_xd_xn(2, 0, 0)); pc += 4
        words.append(enc_ldr_xd_xn(3, 0, 8)); pc += 4
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


def load_manifest(path):
    import json
    return json.loads(Path(path).read_text())


def verify_kernel(kernel_path, manifest_path, mode):
    """按 manifest 校验内核。mode='base' 查基准内核，mode='patched' 查打完的内核。

    这样 flash-prep.sh 不需要自己维护第二份地址/指令常量，
    所有常量只在补丁脚本里出现一次（避免两处配置漂移，见 m07787）。
    """
    man = load_manifest(manifest_path)
    data = bytearray(Path(kernel_path).read_bytes())
    digest = hashlib.sha256(data).hexdigest()
    base = (mode == 'base')
    # 'base' 模式优先用 raw_input_sha256（= 铺进 payload 的那份未打补丁内核），
    # 因为 trace 阶段看到的 input_sha256 已经是打完 PAN/上屏/roothash 的中内核。
    if base:
        want = man.get('raw_input_sha256') or man['input_sha256']
    else:
        want = man['output_sha256']
    label = '基准内核' if base else '已补丁内核'
    if digest != want:
        print(f'  !! {label} sha256 不符\n  !!   实际 {digest}\n  !!   期望 {want}')
        return 4
    print(f'  {label} sha256 ✓ {digest}')

    segs, _ = parse_segments(data)
    if base:
        if MAGIC in data:
            print('  !! 基准内核里已经有 R11TRACE 魔数 —— 这不是干净内核')
            return 4
        for p in man['probes']:
            off = va_to_off(segs, p['hook_va'])
            if off is None:
                print(f'  !! {p["name"]} 的地址 {p["hook_va"]:#x} 不在文件里')
                return 4
            got = struct.unpack_from('<I', data, off)[0]
            if got != p['orig_word']:
                print(f'  !! {p["name"]} @ {p["hook_va"]:#x}: {got:#010x} != {p["orig_word"]:#010x}')
                return 4
            print(f'  {p["name"]} @ {p["hook_va"]:#x} 原始指令 ✓ {got:#010x}')
        return 0

    for p in man['probes']:
        off = va_to_off(segs, p['hook_va'])
        got = struct.unpack_from('<I', data, off)[0]
        want_b = enc_b(p['hook_va'], p['tramp_va'])
        if got != want_b:
            print(f'  !! {p["name"]} @ {p["hook_va"]:#x}: {got:#010x} '
                  f'不是 b {p["tramp_va"]:#x}（期望 {want_b:#010x}）')
            return 4
        toff = va_to_off(segs, p['tramp_va'])
        head = struct.unpack_from('<I', data, toff)[0]
        if head != 0xA9BF7BFD:
            print(f'  !! {p["name"]} 的跳板 @ {p["tramp_va"]:#x} 开头不是 stp x29,x30')
            return 4
        print(f'  {p["name"]} 跳板 ✓ {p["hook_va"]:#x} -> {p["tramp_va"]:#x}')
    return 0


def main():
    argv = sys.argv[1:]
    symbols_path = None
    probes_spec = DEFAULT_PROBES
    manifest_path = None
    verify_mode = None
    positional = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ('--symbols', '--probes', '--manifest') and i + 1 < len(argv):
            value = argv[i + 1]
            if a == '--symbols':
                symbols_path = value
            elif a == '--manifest':
                manifest_path = value
            else:
                probes_spec = value
            i += 2
            continue
        if a.startswith('--symbols='):
            symbols_path = a.split('=', 1)[1]
        elif a.startswith('--probes='):
            probes_spec = a.split('=', 1)[1]
        elif a.startswith('--manifest='):
            manifest_path = a.split('=', 1)[1]
        elif a in ('--verify', '--verify-base'):
            verify_mode = 'base' if a == '--verify-base' else 'patched'
        elif not a.startswith('--'):
            positional.append(a)
        i += 1
    if verify_mode:
        if not positional or not manifest_path:
            print('!! --verify/--verify-base 需要 <kernel> 与 --manifest <path>')
            return 2
        return verify_kernel(positional[0], manifest_path, verify_mode)
    if not positional or not symbols_path:
        print(__doc__)
        return 2
    src = Path(positional[0])
    dst = Path(positional[1]) if len(positional) > 1 else src
    data = bytearray(src.read_bytes())
    before = hashlib.sha256(data).hexdigest()
    print(f'  输入: {src}')
    print(f'  输入 sha256: {before}')
    segs, _ = parse_segments(data)

    if MAGIC in data:
        # 以前这里是"静默跳过"。结果：payload 里没重铺干净内核时，
        # 上一轮的旧探针会继续生效，屏幕上打出来的是旧地址/旧参数的
        # 结果 —— 白白烧掉两轮刷机（lret 一直是野值就是这么来的）。
        # 现在默认直接失败，逼出真正的原因。
        if '--allow-existing' in argv:
            print('  已经装过 R11TRACE 探针（--allow-existing，跳过）')
            if dst != src:
                dst.write_bytes(data)
            return 0
        print('  !! 这个内核里已经有 R11TRACE 探针（旧的一轮）。')
        print('  !! 旧钩子不会因为这次调用而消失，屏幕上打出来的会是旧探针的结果。')
        print('  !! 请先把干净内核铺回 payload，再打补丁，例如：')
        print("  !!   mcopy -o -i <payload.img> <art>/kernel/kernel.macho '::/EFI/UniXNU/Payloads/kernel.macho'")
        print('  !! 确实想保留旧探针、只重打 payload 的其它补丁，加 --allow-existing。')
        return 3

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
        if name in EXPECT:
            want_va, want_word = EXPECT[name]
            if hook_va != want_va:
                raise SystemExit(f'!! {name} 的目标地址是 {hook_va:#x}，'
                                 f'但期望 {want_va:#x} —— 内核版本不对，停止')
            off = va_to_off(segs, hook_va)
            got = struct.unpack_from('<I', data, off)[0]
            if got != want_word:
                raise SystemExit(f'!! {name} @ {hook_va:#x} 的原始指令是 {got:#010x}，'
                                 f'期望 {want_word:#010x} —— 不是同一段代码，停止')
            print(f'  校验 {name} @ {hook_va:#x}: 原始指令 {got:#010x} ✓')
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
    out_sha = hashlib.sha256(data).hexdigest()
    print(f'  输出 sha256: {out_sha}（输入 {before}）')

    # 导出本次构建的 manifest：后续 flash-prep.sh 只认它，不再自带常量副本。
    import json
    man = dict(
        schema=1,
        script='patch-kernel-trace.py',
        probes_spec=probes_spec,
        cave_va=cave_va,
        input=str(src), input_sha256=before,
        output=str(dst), output_sha256=out_sha,
        probes=[dict(name=h['name'], kind=h['kind'], hook_va=h['va'],
                     orig_word=h['orig'], tramp_va=h['tramp_va'], fmt_va=h['fmt_va'])
                for h in hooks],
    )
    mpath = Path(manifest_path) if manifest_path else dst.with_name(dst.name + '.manifest.json')
    mpath.write_text(json.dumps(man, indent=2) + '\n')
    print(f'  manifest: {mpath}')
    print('  探针: ' + ' / '.join(f'{h["kind"]}@{h["tramp_va"]:#x}(msg {h["fmt_va"]:#x})'
                                 for h in hooks))
    return 0


if __name__ == '__main__':
    sys.exit(main())
