#!/usr/bin/env python3
"""把 QEMU `-d in_asm` 轨迹的尾部映射到内核符号，定位 panic 前的执行路径。

用法:
  llvm-nm -n /tmp/k.macho > /tmp/ksyms.txt
  python3 analyze-inasm.py <inasm 文件> <ksyms 文件> [尾部条数，默认 80]

输出：最后 N 个唯一 PC，按执行顺序，每个带「符号 + 偏移」。
这样能看到内核在 panic 前依次调用了哪些函数（例如 exec_mach_imgact → load_machfile → ...）。
"""
import bisect
import re
import sys
from pathlib import Path

PC_RE = re.compile(r'^\s*0x([0-9a-fA-F]{8,16}):\s')


def load_syms(path: Path):
    addrs, names = [], []
    for line in path.read_text(errors='replace').splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        try:
            addr = int(parts[0], 16)
        except ValueError:
            continue
        if not parts[1] in ('T', 't', 'S', 's', 'D', 'd', 'B', 'b', 'W', 'w'):
            continue
        addrs.append(addr)
        names.append(parts[2])
    order = sorted(range(len(addrs)), key=lambda i: addrs[i])
    return [addrs[i] for i in order], [names[i] for i in order]


def resolve(addrs, names, pc):
    i = bisect.bisect_right(addrs, pc) - 1
    if i < 0:
        return None, 0
    return names[i], pc - addrs[i]


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    inasm = Path(sys.argv[1])
    syms = Path(sys.argv[2])
    tail = int(sys.argv[3]) if len(sys.argv) > 3 else 80

    addrs, names = load_syms(syms)
    print(f'符号数: {len(addrs)}')

    pcs = []
    with inasm.open('r', errors='replace') as f:
        for line in f:
            m = PC_RE.match(line)
            if m:
                pc = int(m.group(1), 16)
                if not pcs or pcs[-1] != pc:
                    pcs.append(pc)

    print(f'轨迹中的基本块数: {len(pcs)}')
    print(f'\n最后 {tail} 个唯一基本块（按执行顺序）：')
    for pc in pcs[-tail:]:
        name, off = resolve(addrs, names, pc)
        if name is None:
            print(f'  {pc:#018x}  <未知>')
        else:
            print(f'  {pc:#018x}  {name}+0x{off:x}')

    # 统计尾部出现的函数（顺带给出调用热点）
    from collections import Counter
    counter = Counter()
    for pc in pcs[-tail:]:
        name, _ = resolve(addrs, names, pc)
        if name:
            counter[name] += 1
    print('\n尾部函数分布（前 15）：')
    for name, count in counter.most_common(15):
        print(f'  {count:4d}  {name}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
