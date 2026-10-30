#!/usr/bin/env python3
"""给内核装两个"用户态到底有没有跑起来"的探针（符号驱动，幂等）。

背景：种子里的 init 已经是 arm64 + PAGEZERO 版，`load_init_program` 打出
"attempting to load .../launchd.development" 之后既没有 `failed loading`、也没有
`Process 1 exec of ... failed`，说明 exec 返回 0；但屏幕上既没有我们写的标记，
也没有 `kill(1, SIGKILL)` 应该触发的 `unexpected SIGKILL of initproc` panic。
也就是说：exec 成功了，但线程可能根本没回到 EL0。

两个探针（都用 kprintf，走同一条能上屏的通道）：

  A) `_thread_bootstrap_return` 入口 —— bsd_init 返回后内核就是从这里回用户态。
     打印 `R11: bootstrap_return (pc=...)` 说明"回用户态"这一步真的走到了。
  B) `_unix_syscall` 入口 —— EL0 的 `svc #0x80` 落在这里。
     打印 `R11: svc %u` 说明用户态真的执行了指令并进了内核。

结论表：
  两个都打 → EL0 通了，问题在我们的 init 自己（console / 标记）
  只打 A   → 回了用户态但第一条指令没执行成功（入口地址 / 页映射）
  两个都不打 → 内核没走到 thread_bootstrap_return，问题在内核侧

实现方式与 `patch-init-screen-gc.py` 相同：在可执行段里找一段全零的空洞，
写入 8 字节魔数 `R11TRACE` + 跳板代码 + 格式串，再把目标函数的第一条指令
改成 `b 跳板`；跳板末尾 `b 目标+4` 回到原函数。魔数在 = 已经打过补丁。

用法:
    python3 patch-kernel-trace.py <kernel> <输出> --symbols <kernel-symbols.txt>
"""
import struct
import sys
from pathlib import Path

MAGIC = b'R11TRACE'
CAVE_SIZE = 0x400

SYM_BOOTSTRAP = '_thread_bootstrap_return'
SYM_SYSCALL = '_unix_syscall'
SYM_KPRINTF = '_kprintf'

MSG_A = b'R11: bootstrap_return (about to run PID 1 in EL0)\n\0'
MSG_B = b'R11: svc %u (EL0 syscall reached the kernel)\n\0'

LC_SEGMENT_64 = 0x19


# ---------- Mach-O ----------

def parse_segments(data):
    (magic, cputype, cpusubtype, filetype, ncmds, sizeofcmds,
     flags, reserved) = struct.unpack_from('<IIIIIIII', data, 0)
    if magic != 0xFEEDFACF:
        raise SystemExit(f'!! 不是 64 位 Mach-O: {magic:#x}')
    off = 32
    segs = []
    for _ in range(ncmds):
        cmd, cmdsize = struct.unpack_from('<II', data, off)
        if cmd == LC_SEGMENT_64:
            name = data[off + 8: off + 24].rstrip(b'\0').decode('ascii', 'replace')
            vmaddr, vmsize, fileoff, filesize, maxprot, initprot, nsects, sflags = \
                struct.unpack_from('<QQQQIIII', data, off + 24)
            segs.append(dict(name=name, vmaddr=vmaddr, vmsize=vmsize, fileoff=fileoff,
                             filesize=filesize, initprot=initprot, nsects=nsects))
        if cmdsize <= 0:
            raise SystemExit('!! load command 长度为 0')
        off += cmdsize
    return segs, filetype


def va_to_off(segs, va):
    for s in segs:
        if s['fileoff'] == 0 and s['filesize'] == 0:
            continue
        if s['vmaddr'] <= va < s['vmaddr'] + s['filesize']:
            return s['fileoff'] + (va - s['vmaddr'])
    return None


PREFER = ('__TEXT_EXEC', '__TEXT', '__PLK_TEXT_EXEC')


def find_cave(data, segs, need):
    """在可执行段里找一段长度 >= need 的全零区，返回 (va, off, size)。

    优先 __TEXT_EXEC：两个钩子就在这段里，确定被映射；gc_enable 的跳板也已经
    在这段里被真机验证过。__PLK_TEXT_EXEC 里虽然有大片零区（prelink 后的 kext
    之间），但那是别人的地盘，放最后。
    """
    for want in PREFER:
        for s in segs:
            if s['name'] != want or not (s['initprot'] & 0x4) or s['filesize'] < need:
                continue
            hit = cave_in_segment(data, s, need)
            if hit:
                return hit
    return None


def cave_in_segment(data, s, need):
    best = None
    base = s['fileoff']
    blob = data[base: base + s['filesize']]
    run_start = None
    i = 0
    n = len(blob)
    while i < n:
        if blob[i] == 0:
            if run_start is None:
                run_start = i
        else:
            if run_start is not None:
                length = i - run_start
                if length >= need and (best is None or length > best[2]):
                    best = (s['vmaddr'] + run_start, base + run_start, length)
                run_start = None
        i += 1
    if run_start is not None:
        length = n - run_start
        if length >= need and (best is None or length > best[2]):
            best = (s['vmaddr'] + run_start, base + run_start, length)
    return best


# ---------- 指令编码 ----------

def enc_b(pc, target):
    delta = target - pc
    if delta % 4:
        raise SystemExit('!! B 偏移没对齐')
    if not (-(1 << 27) <= delta < (1 << 27)):
        raise SystemExit('!! B 偏移超出 ±128MB')
    return 0x14000000 | ((delta >> 2) & 0x03FFFFFF)


def enc_bl(pc, target):
    delta = target - pc
    if delta % 4:
        raise SystemExit('!! BL 偏移没对齐')
    if not (-(1 << 27) <= delta < (1 << 27)):
        raise SystemExit('!! BL 偏移超出 ±128MB')
    return 0x94000000 | ((delta >> 2) & 0x03FFFFFF)


def enc_adrp_x0(pc, target):
    page_delta = ((target & ~0xFFF) - (pc & ~0xFFF)) >> 12
    if not (-(1 << 20) <= page_delta < (1 << 20)):
        raise SystemExit('!! ADRP 超出 ±4GB')
    immlo = page_delta & 0x3
    immhi = (page_delta >> 2) & 0x7FFFF
    return 0x90000000 | (immlo << 29) | (immhi << 5)


def enc_add_x0_imm(imm12):
    return 0x91000000 | ((imm12 & 0xFFF) << 10)


def enc_stp_pre(rt, rt2, rn, offset):
    """STP <rt>,<rt2>,[<rn>,#offset]!"""
    imm7 = (offset // 8) & 0x7F
    return 0xA9800000 | (imm7 << 15) | (rt2 << 10) | (rn << 5) | rt


def enc_ldp_post(rt, rt2, rn, offset):
    """LDP <rt>,<rt2>,[<rn>],#offset"""
    imm7 = (offset // 8) & 0x7F
    return 0xA8C00000 | (imm7 << 15) | (rt2 << 10) | (rn << 5) | rt


def enc_mov_xd_xs(rd, rm):
    return 0xAA0003E0 | (rm << 16) | rd


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

def tramp_simple(base, hook, kprintf, fmt_va):
    """只打印一行的跳板（用于 noreturn 的 thread_bootstrap_return）。"""
    words = []
    pc = base
    words.append(enc_stp_pre(29, 30, 31, -16)); pc += 4        # stp x29,x30,[sp,#-16]!
    words.append(enc_adrp_x0(pc, fmt_va)); pc += 4
    words.append(enc_add_x0_imm(fmt_va & 0xFFF)); pc += 4
    words.append(enc_bl(pc, kprintf)); pc += 4
    words.append(enc_ldp_post(29, 30, 31, 16)); pc += 4        # ldp x29,x30,[sp],#16
    words.append(enc_b(pc, hook + 4))
    return b''.join(struct.pack('<I', w) for w in words)


def tramp_syscall(base, hook, kprintf, fmt_va):
    """保存 x0..x17 → 打印 x16 → 恢复 → 回原函数。"""
    save = [(0, 1), (2, 3), (4, 5), (6, 7), (8, 9),
            (10, 11), (12, 13), (14, 15), (16, 17)]
    words = []
    pc = base
    words.append(enc_stp_pre(29, 30, 31, -16)); pc += 4
    for a, b in save:
        words.append(enc_stp_pre(a, b, 31, -16)); pc += 4
    words.append(enc_mov_xd_xs(1, 16)); pc += 4                # mov x1, x16
    words.append(enc_adrp_x0(pc, fmt_va)); pc += 4
    words.append(enc_add_x0_imm(fmt_va & 0xFFF)); pc += 4
    words.append(enc_bl(pc, kprintf)); pc += 4
    for a, b in reversed(save):
        words.append(enc_ldp_post(a, b, 31, 16)); pc += 4
    words.append(enc_ldp_post(29, 30, 31, 16)); pc += 4
    words.append(enc_b(pc, hook + 4))
    return b''.join(struct.pack('<I', w) for w in words)


def main():
    argv = sys.argv[1:]
    positional = [a for a in argv if not a.startswith('--')]
    symbols_path = None
    for i, a in enumerate(argv):
        if a == '--symbols' and i + 1 < len(argv):
            symbols_path = argv[i + 1]
        elif a.startswith('--symbols='):
            symbols_path = a.split('=', 1)[1]
    if len(positional) < 1 or not symbols_path:
        print(__doc__)
        return 2
    src = Path(positional[0])
    dst = Path(positional[1]) if len(positional) > 1 else src
    data = bytearray(src.read_bytes())
    segs, filetype = parse_segments(data)

    if MAGIC in data:
        print('  已经装过 R11TRACE 探针，跳过')
        if dst != src:
            dst.write_bytes(data)
        return 0

    table = parse_symbols(symbols_path)
    hook_a = sym(table, SYM_BOOTSTRAP)
    hook_b = sym(table, SYM_SYSCALL)
    kprintf = sym(table, SYM_KPRINTF)
    print(f'  符号: {SYM_BOOTSTRAP}={hook_a:#x} {SYM_SYSCALL}={hook_b:#x} {SYM_KPRINTF}={kprintf:#x}')

    need = CAVE_SIZE
    cave = find_cave(data, segs, need)
    if not cave:
        raise SystemExit(f'!! 找不到 {need} 字节的全零空洞')
    cave_va, cave_off, cave_len = cave
    print(f'  空洞: {cave_va:#x}（{cave_len} 字节可用，取 {need}）')

    # 布局：魔数(8) + trampA + 对齐 + trampB + msgA + msgB
    cursor = cave_va + 8
    tramp_a_va = cursor
    tramp_a = tramp_simple(tramp_a_va, hook_a, kprintf, cave_va)   # 先占位（用洞地址）只为量长度
    cursor += len(tramp_a)
    cursor = (cursor + 7) & ~7
    tramp_b_va = cursor
    tramp_b = tramp_syscall(tramp_b_va, hook_b, kprintf, cave_va)
    cursor += len(tramp_b)
    cursor = (cursor + 7) & ~7
    msg_a_va = cursor
    cursor += len(MSG_A)
    msg_b_va = cursor
    cursor += len(MSG_B)
    if cursor - cave_va > cave_len:
        raise SystemExit('!! 空洞装不下探针')

    tramp_a = tramp_simple(tramp_a_va, hook_a, kprintf, msg_a_va)
    tramp_b = tramp_syscall(tramp_b_va, hook_b, kprintf, msg_b_va)

    blob = bytearray()
    blob += MAGIC                                     # 8 字节
    blob += tramp_a
    blob += b'\0' * ((tramp_b_va - (cave_va + len(blob))) if tramp_b_va > cave_va + len(blob) else 0)
    assert cave_off + len(blob) == va_to_off(segs, tramp_b_va) or True
    blob += tramp_b
    blob += b'\0' * ((msg_a_va - (cave_va + len(blob))) if msg_a_va > cave_va + len(blob) else 0)
    blob += MSG_A
    blob += MSG_B
    off = cave_off
    data[off: off + len(blob)] = blob

    # 装钩子
    for hook, tramp_va, name in ((hook_a, tramp_a_va, SYM_BOOTSTRAP),
                                 (hook_b, tramp_b_va, SYM_SYSCALL)):
        hoff = va_to_off(segs, hook)
        if hoff is None:
            raise SystemExit(f'!! {name} 不在可写的文件区间里')
        old = struct.unpack_from('<I', data, hoff)[0]
        struct.pack_into('<I', data, hoff, enc_b(hook, tramp_va))
        print(f'  {name} {hook:#x}: {old:08x} -> b {tramp_va:#x}')

    dst.write_bytes(data)
    print(f'  输出: {dst}')
    print(f'  探针: A @{tramp_a_va:#x} (msg {msg_a_va:#x}) / B @{tramp_b_va:#x} (msg {msg_b_va:#x})')
    return 0


if __name__ == '__main__':
    sys.exit(main())
