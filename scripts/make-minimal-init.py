#!/usr/bin/env python3
"""构建一个最小的静态 ARM64 Mach-O init 程序（不依赖 SDK / macOS）。

用途：放进 Data seed 的 /sbin/launchd 与 /usr/appleinternal/sbin/launchd.development，
让 XNU 挂上根文件系统后能 exec 成功，从而验证 EL0 用户态执行链路。

程序行为（手写指令，无汇编器）：
  1. open("/dev/console", O_WRONLY)             XNU 系统调用 5
  2. 成功则 write(fd, "R11_EL0_INIT_OK\\n", n)  系统调用 4
  3. 再 write(1, "R11_EL0_INIT_OK\\n", n)
  4. 死循环（保持 PID 1 存活，避免内核 panic）

XNU arm64 系统调用约定：
  x16 = 调用号，参数 x0..x7，`svc #0x80`，返回 x0，出错置 carry（b.cs 跳过）

用法:
  python3 make-minimal-init.py [输出路径，默认 build/init.macho]
"""
import struct
import sys
from pathlib import Path

VMADDR = 0x100000000
PAGEZERO_SIZE = 0x100000000   # arm64 可执行文件必须的 hard page zero（4 GiB）
MH_MAGIC_64 = 0xFEEDFACF
CPU_TYPE_ARM64 = 0x0100000C
CPU_SUBTYPE_ARM64_ALL = 0x00000000   # 注意：2 是 CPU_SUBTYPE_ARM64E！
MH_EXECUTE = 2
LC_SEGMENT_64 = 0x19
LC_UNIXTHREAD = 0x5
ARM_THREAD_STATE64 = 6
ARM_THREAD_STATE64_COUNT = 68

MARKER = b'R11_EL0_INIT_OK\n'
DEVPATH = b'/dev/console\0'


def movz_x(rd, imm16):
    return 0xD2800000 | ((imm16 & 0xFFFF) << 5) | rd


def movz_w(rd, imm16):
    return 0x52800000 | ((imm16 & 0xFFFF) << 5) | rd


def mov_xd_xs(rd, rm):
    return 0xAA0003E0 | (rm << 16) | rd


def svc(imm=0x80):
    return 0xD4000001 | ((imm & 0xFFFF) << 5)


def adr(rd, imm):
    if not (-(1 << 20) <= imm < (1 << 20)):
        raise ValueError(f'ADR 偏移超范围: {imm}')
    immlo = imm & 0x3
    immhi = (imm >> 2) & 0x7FFFF
    return 0x10000000 | (immlo << 29) | (immhi << 5) | rd


def b_cond(cond, imm):
    if imm % 4:
        raise ValueError('分支偏移必须 4 字节对齐')
    return 0x54000000 | (((imm >> 2) & 0x7FFFF) << 5) | cond


def build_code():
    # 指令表：(助记符, 参数) —— ADR/分支的目标先占位，稍后按最终长度回填
    LATEST = 17          # b . 的索引
    L_CONSOLE_WRITE = 12  # console 写入失败后跳到「写 fd 1」的位置

    instrs = [None] * 18
    instrs[0] = ('adr', 0, 'devpath')
    instrs[1] = ('raw', movz_w(1, 1))         # x1 = O_WRONLY
    instrs[2] = ('raw', movz_x(2, 0))         # x2 = 0
    instrs[3] = ('raw', movz_x(16, 5))        # x16 = SYS_open
    instrs[4] = ('raw', svc())
    instrs[5] = ('bcond', 2, L_CONSOLE_WRITE)  # b.cs → 跳过 console 写入
    instrs[6] = ('raw', mov_xd_xs(3, 0))      # x3 = x0（console fd）
    instrs[7] = ('adr', 1, 'marker')
    instrs[8] = ('raw', movz_x(2, len(MARKER)))
    instrs[9] = ('raw', mov_xd_xs(0, 3))      # x0 = x3
    instrs[10] = ('raw', movz_x(16, 4))       # x16 = SYS_write
    instrs[11] = ('raw', svc())
    instrs[12] = ('adr', 1, 'marker')
    instrs[13] = ('raw', movz_x(2, len(MARKER)))
    instrs[14] = ('raw', movz_x(0, 1))        # fd 1
    instrs[15] = ('raw', movz_x(16, 4))       # x16 = SYS_write
    instrs[16] = ('raw', svc())
    instrs[17] = ('raw', 0x14000000)          # b .

    code_len = len(instrs) * 4
    msg_off = code_len
    dev_off = msg_off + len(MARKER)

    words = []
    for index, item in enumerate(instrs):
        if item[0] == 'raw':
            words.append(item[1])
        elif item[0] == 'adr':
            rd, label = item[1], item[2]
            target = msg_off if label == 'marker' else dev_off
            words.append(adr(rd, target - index * 4))
        elif item[0] == 'bcond':
            cond, target_index = item[1], item[2]
            words.append(b_cond(cond, (target_index - index) * 4))

    instr = b''.join(struct.pack('<I', w) for w in words)
    data = MARKER + DEVPATH
    return instr, data, msg_off, dev_off


def build_macho():
    code, data, msg_off, dev_off = build_code()

    pagezero_cmd_size = 72           # LC_SEGMENT_64，无 section
    seg_cmd_size = 72 + 80           # __TEXT + 一个 __text section
    th_cmd_size = 8 + 8 + 34 * 8     # cmd+cmdsize + flavor+count + 34 个 64 位寄存器
    sizeofcmds = pagezero_cmd_size + seg_cmd_size + th_cmd_size
    code_off = (32 + sizeofcmds + 7) & ~7
    entry = VMADDR + code_off
    filesize = code_off + len(code) + len(data)
    vmsize = (filesize + 0xFFF) & ~0xFFF

    hdr = struct.pack('<IIIIIIII', MH_MAGIC_64, CPU_TYPE_ARM64, CPU_SUBTYPE_ARM64_ALL,
                      MH_EXECUTE, 3, sizeofcmds, 0, 0)

    sect = struct.pack('<16s16sQQIIIIIIII', b'__text', b'__TEXT',
                       entry, len(code), code_off, 2, 0, 0, 0, 0x80000400, 0, 0)
    # filesize 必须是真实内容长度（不能填 vmsize，否则内核读段会越过文件末尾）
    # arm64 可执行文件必须有 4 GiB 的 __PAGEZERO（vm_map_has_hard_pagezero 检查）
    pagezero = struct.pack('<II16sQQQQIIII', LC_SEGMENT_64, pagezero_cmd_size, b'__PAGEZERO',
                           0, PAGEZERO_SIZE, 0, 0, 0, 0, 0, 0)
    seg = struct.pack('<II16sQQQQIIII', LC_SEGMENT_64, seg_cmd_size, b'__TEXT',
                      VMADDR, vmsize, 0, filesize, 5, 5, 1, 0)

    th = struct.pack('<IIII', LC_UNIXTHREAD, th_cmd_size - 8,
                     ARM_THREAD_STATE64, ARM_THREAD_STATE64_COUNT)
    state = [0] * 34
    state[32] = entry
    th += b''.join(struct.pack('<Q', v & 0xFFFFFFFFFFFFFFFF) for v in state)

    cmds = pagezero + seg + sect + th
    body = hdr + cmds
    body += b'\0' * (code_off - len(body))
    body += code + data
    return body, entry, code_off, len(code)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    pad = 0
    for a in sys.argv[1:]:
        if a.startswith('--pad='):
            pad = int(a.split('=', 1)[1], 0)
    out = Path(args[0]) if args else Path('build/init.macho')
    out.parent.mkdir(parents=True, exist_ok=True)
    blob, entry, code_off, instr_len = build_macho()
    if pad and len(blob) < pad:
        # 填充到指定大小：避免 APFS 把小文件内联存放在 inode 里，
        # 那样 iOS 的 APFS 读取路径可能返回元数据块而不是文件内容。
        blob = bytes(blob) + b'\0' * (pad - len(blob))
    out.write_bytes(blob)
    print(f'已生成 {out}  ({len(blob)} B)')
    print(f'  入口 VA     : 0x{entry:016X}（文件偏移 0x{code_off:X}）')
    print(f'  指令长度    : {instr_len} B（{instr_len // 4} 条）')
    print(f'  标记串      : {MARKER!r}')
    if pad:
        print(f'  已填充到    : {len(blob)} B（--pad={pad}）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
