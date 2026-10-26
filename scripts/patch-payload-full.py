#!/usr/bin/env python3
"""给 payload 卷里的 kernel 打全套补丁（含内核日志上屏）。

流程：
  1. 从 payload.img 用 mtools 取出 kernel.macho
  2. 打内核补丁（PAN / CPU 校验 / 输出开关 / serial_init / cons_ops_index）
  3. 打"initialize_screen 返回前调用 gc_enable(TRUE)"的跳板补丁
  4. 写回 payload.img

用法:
  python3 patch-payload-full.py <payload.img>
"""
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent


def run(cmd, quiet=False):
    r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True)
    if not quiet:
        out = (r.stdout or '') + (r.stderr or '')
        for line in out.strip().splitlines():
            print('   ', line)
    if r.returncode != 0:
        print('!! 命令失败:', ' '.join(str(c) for c in cmd))
    return r


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    payload = Path(sys.argv[1])
    tmp = Path(tempfile.mkdtemp(prefix='r11k-'))
    kernel = tmp / 'kernel.macho'

    # kernel 在 payload 卷的哪里？先列一下
    r = run(['mdir', '-i', str(payload), '-/'])
    target = None
    for line in (r.stdout or '').splitlines():
        if 'kernel' in line.lower() and '.macho' in line.lower():
            target = line.strip()
    print(f'payload 里的 kernel: {target}')

    # 已知布局: /Payloads/kernel.macho 或 /kernel.macho，逐个试
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
    # 注意：必须用 patch-kernel-minimal.py 并带齐三组开关。
    # patch-kernel-full.py 只认 msr PAN,#1（0xD500419F），漏掉了更常见的
    # msr PAN,#0（0xD500409F，_copyout 里那条）—— 那正是真机上第一个
    # Undefined kernel instruction 的位置。这里改成 minimal + 全开关。
    if run(['python3', SCRIPTS / 'patch-kernel-minimal.py', kernel, kernel,
            '--with-validate', '--with-trapfix', '--with-mrs1']).returncode != 0:
        return 1
    print('== 打内核日志上屏补丁')
    if run(['python3', SCRIPTS / 'patch-init-screen-gc.py', kernel]).returncode != 0:
        return 1

    print('== 写回 payload')
    if run(['mcopy', '-o', '-i', str(payload), str(kernel), got]).returncode != 0:
        return 1
    print('完成')
    return 0


if __name__ == '__main__':
    sys.exit(main())
