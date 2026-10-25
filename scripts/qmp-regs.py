#!/usr/bin/env python3
"""通过 QMP 读取 CPU 寄存器，定位内核/用户态此刻停在哪里。

用法: python3 qmp-regs.py <qmp-socket> [期望 PC，例如 0x1000001d8]

HMP 的 `info registers` 输出里 PC/CPSR 各自成行；多核时按 CPU 分块。
本脚本遍历所有 CPU，打印每个 PC，并给出：
  - 落在 0x100000000+ → 用户态 init 映像
  - 落在 0xFFFFFFF0... → 内核
"""
import json
import re
import socket
import sys
import time


def connect(path):
    for _ in range(20):
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(5)
            s.connect(path)
            return s
        except OSError:
            time.sleep(0.5)
    return None


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    path = sys.argv[1]
    want = int(sys.argv[2], 16) if len(sys.argv) > 2 else None

    s = connect(path)
    if s is None:
        print('!! 连不上 QMP')
        return 1
    f = s.makefile('rwb')

    def cmd(obj):
        f.write((json.dumps(obj) + '\n').encode())
        f.flush()
        try:
            return json.loads(f.readline().decode(errors='replace'))
        except Exception:
            return {}

    cmd({'execute': 'qmp_capabilities'})
    info = cmd({'execute': 'query-cpus-fast'})
    cpus = info.get('return', []) if isinstance(info, dict) else []
    n_cpu = len(cpus) if cpus else 4
    print(f'CPU 数量: {n_cpu}（来自 query-cpus-fast 或默认 4）')

    all_pcs = []
    for idx in range(n_cpu):
        # 选中该 CPU 再读寄存器
        sel = cmd({'execute': 'human-monitor-command',
                   'arguments': {'command-line': f'cpu {idx}'}})
        r = cmd({'execute': 'human-monitor-command',
                 'arguments': {'command-line': 'info registers'}})
        text = r.get('return', '') if isinstance(r, dict) else ''
        if not text:
            print(f'  CPU{idx}: 读取失败')
            continue
        pcs = re.findall(r'PC=([0-9a-fA-F]+)', text)
        cpsrs = re.findall(r'CPSR=([0-9a-fA-F]+)', text)
        sps = re.findall(r'SP=([0-9a-fA-F]+)', text)
        print(f'  CPU{idx}: PC={pcs} SP={sps} CPSR={cpsrs}')
        for pc_hex in pcs:
            all_pcs.append((idx, int(pc_hex, 16)))
        for c in cpsrs:
            el = int(c, 16) & 0xF
            if el == 0:
                print(f'    ★ CPU{idx} 处于 EL0（用户态）')

    print()
    hit = False
    for idx, pc in all_pcs:
        where = '未知'
        if 0x100000000 <= pc < 0x100100000:
            where = '用户态 init 映像 ★'
            hit = True
        elif pc >= 0xFFFFFFF000000000:
            where = '内核'
        elif 0x48000000 <= pc < 0x50000000:
            where = '内核物理区(MMU off?)'
        print(f'  CPU{idx} PC={pc:#018x}  → {where}')
        if want is not None and pc == want:
            print(f'    ★★ 正好是期望地址 {want:#x}')
            hit = True

    print('\n结论:', '用户态 init 在运行 ✅' if hit else '未在用户态 ❌')
    return 0 if hit else 1


if __name__ == '__main__':
    sys.exit(main())
