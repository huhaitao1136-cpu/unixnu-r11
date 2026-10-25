#!/usr/bin/env python3
"""修改 payload 里 unixnu.toml 的 [boot] timeout。

用法:
  python3 patch-payload-timeout.py <payload.img> [秒数]

没有 [boot] 段就新建；有 timeout 就替换。
"""
import re
import subprocess
import sys
import tempfile
from pathlib import Path


def run(cmd):
    r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True)
    if r.returncode != 0:
        print('!! 命令失败:', ' '.join(str(c) for c in cmd))
        print((r.stderr or r.stdout)[:300])
    return r


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    img = Path(sys.argv[1])
    secs = sys.argv[2] if len(sys.argv) > 2 else '15'

    tmp = Path(tempfile.mkdtemp(prefix='r11to-'))
    src = tmp / 'unixnu.toml'
    dst = tmp / 'unixnu-new.toml'

    if run(['mcopy', '-o', '-i', str(img), '::/EFI/UniXNU/unixnu.toml', str(src)]).returncode != 0:
        return 1
    text = src.read_text(encoding='utf-8', errors='replace')

    if re.search(r'^\s*timeout\s*=', text, re.M):
        new = re.sub(r'^(\s*timeout\s*=\s*).*$', lambda m: f'{m.group(1)}{secs}', text, count=1, flags=re.M)
    elif re.search(r'^\s*\[boot\]', text, re.M):
        new = re.sub(r'^(\s*\[boot\]\s*)$', lambda m: f'{m.group(1)}\ntimeout = {secs}', text, count=1, flags=re.M)
    else:
        new = text.rstrip() + f'\n\n[boot]\ntimeout = {secs}\n'

    dst.write_text(new, encoding='utf-8')
    print('---- 新 toml（timeout 相关行）----')
    for line in new.splitlines():
        if 'timeout' in line or line.strip().startswith('['):
            print(' ', line)

    if run(['mcopy', '-o', '-i', str(img), str(dst), '::/EFI/UniXNU/unixnu.toml']).returncode != 0:
        return 1
    print('已写回 payload')
    return 0


if __name__ == '__main__':
    sys.exit(main())
