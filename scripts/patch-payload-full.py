#!/usr/bin/env python3
"""给 payload 卷里的 kernel 打全套补丁（含内核日志上屏）。

流程：
  1. 从 payload.img 用 mtools 取出 kernel.macho
  2. 打内核补丁（PAN / CPU 校验 / 输出开关 / serial_init / cons_ops_index）
  3. 打"initialize_screen 之后调用 gc_enable(TRUE)"的跳板补丁（需要符号表）
  4. 写回 payload.img

符号表（kernel-symbols.txt）由 CI 用 llvm-nm 从
`mach.development.bcm2837.unstripped.noctf` 导出，和 r11-boot-artifacts 一起下载。
不传 --symbols 时会在几个常见位置自动找。

用法:
  python3 patch-payload-full.py <payload.img> [--symbols <kernel-symbols.txt>]
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent

# 自动找符号表的候选位置（按顺序）
SYMBOL_HINTS = [
    '/root/src/UniXNU/out/r11/kernel/kernel-symbols.txt',
    'kernel-symbols.txt',
]


def run(cmd, quiet=False):
    r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True)
    if not quiet:
        out = (r.stdout or '') + (r.stderr or '')
        for line in out.strip().splitlines():
            print('   ', line)
    if r.returncode != 0:
        print('!! 命令失败:', ' '.join(str(c) for c in cmd))
    return r


def find_symbols(explicit, payload):
    if explicit:
        p = Path(explicit)
        return p if p.is_file() else None
    for c in SYMBOL_HINTS:
        p = Path(c)
        if p.is_file() and p.stat().st_size > 0:
            return p
    # payload 同目录、以及 /tmp/art*/kernel 下
    for pat in ('kernel-symbols.txt', '../kernel/kernel-symbols.txt'):
        for p in payload.parent.glob(pat):
            if p.is_file():
                return p
    for p in sorted(Path('/tmp').glob('art*/kernel/kernel-symbols.txt')):
        if p.is_file():
            return p
    return None


def make_symbols_from_kernel(kernel, out):
    """内核 Mach-O 自带符号表，用 llvm-nm 现导一份，省掉从 CI 拿 kernel-symbols.txt。"""
    nm = None
    for cand in ('llvm-nm', 'llvm-nm-18', 'llvm-nm-19', 'llvm-nm-17', 'llvm-nm-16'):
        p = shutil.which(cand)
        if p:
            nm = p
            break
    if nm is None:
        return None
    r = subprocess.run([nm, '--defined-only', str(kernel)], capture_output=True, text=True)
    if r.returncode != 0:
        return None
    rows = []
    for line in (r.stdout or '').splitlines():
        parts = line.split()
        if len(parts) >= 3:
            try:
                int(parts[0], 16)
            except ValueError:
                continue
            rows.append(f'{parts[0]} {parts[1]} {parts[2]}')
    if not rows:
        return None
    out.write_text('\n'.join(rows) + '\n')
    return out


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
    payload = Path(argv[0])
    tmp = Path(tempfile.mkdtemp(prefix='r11k-'))
    kernel = tmp / 'kernel.macho'

    # kernel 在 payload 卷的哪里？先列一下
    r = run(['mdir', '-i', str(payload), '-/'])
    target = None
    for line in (r.stdout or '').splitlines():
        if 'kernel' in line.lower() and '.macho' in line.lower():
            target = line.strip()
    print(f'payload 里的 kernel: {target}')

    cands = ['::/EFI/UniXNU/Payloads/kernel.macho', '::/Payloads/kernel.macho', '::/kernel.macho']
    got = None
    for c in cands:
        rr = run(['mcopy', '-o', '-i', str(payload), c, str(kernel)], quiet=True)
        if rr.returncode == 0 and kernel.exists():
            got = c
            break
    if got is None:
        print('!! 找不到 kernel.macho，请手工确认路径')
        return 1
    print(f'已取出 {got} ({kernel.stat().st_size} B)')

    print('== 打内核补丁')
    # 必须用 patch-kernel-minimal.py 并带齐三组开关：patch-kernel-full.py 漏掉
    # msr PAN,#0（0xD500409F，_copyout 里那条），那正是真机第一个 undefined instruction。
    if run(['python3', SCRIPTS / 'patch-kernel-minimal.py', kernel, kernel,
            '--with-validate', '--with-trapfix', '--with-mrs1']).returncode != 0:
        return 1

    print('== 打内核日志上屏补丁')
    sym = find_symbols(symbols, payload)
    if sym is None:
        print('   没找到 kernel-symbols.txt，改用 llvm-nm 从内核自己导出')
        sym = make_symbols_from_kernel(kernel, tmp / 'kernel-symbols.txt')
    if sym is None:
        print('!! 没有符号表，也没有 llvm-nm —— 请从 r11-boot-artifacts 里取出 kernel-symbols.txt')
        print('   或显式传 --symbols <path>（CI 的 导出内核符号 步骤会生成它）')
        return 1
    print(f'   符号表: {sym}')
    if run(['python3', SCRIPTS / 'patch-init-screen-gc.py', kernel, kernel,
            '--symbols', sym]).returncode != 0:
        return 1

    print('== 写回 payload')
    if run(['mcopy', '-o', '-i', str(payload), str(kernel), got]).returncode != 0:
        return 1
    print('完成')
    return 0


if __name__ == '__main__':
    sys.exit(main())
