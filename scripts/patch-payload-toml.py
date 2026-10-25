#!/usr/bin/env python3
"""修改 payload 卷里的 unixnu.toml：把 UI 控制台设成 none（保持 headless）。

为什么要这样：我们的 R11FbGop 能让 UniXNU 进入图形模式（ui: graphics），
但它画第一帧时会崩在别的模块里。把控制台设为 none 后 UniXNU 保持 headless
（只打印日志），而 DarwinBoot 仍然直接读 GOP → 内核照样拿到真实帧缓冲。

用法:
  python3 patch-payload-toml.py <payload.img> [none|auto|graphics]
"""
import subprocess
import sys
import tempfile
from pathlib import Path

MODE = {'none': 'none', 'auto': 'auto', 'graphics': 'graphics'}


def run(cmd):
    r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True)
    if r.returncode != 0:
        print('!! 命令失败:', ' '.join(str(c) for c in cmd))
        print((r.stderr or r.stdout)[:400])
    return r


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    img = Path(sys.argv[1])
    mode = sys.argv[2] if len(sys.argv) > 2 else 'none'
    if mode not in MODE:
        print('模式必须是 none / auto / graphics')
        return 1

    tmp = Path(tempfile.mkdtemp(prefix='r11toml-'))
    src = tmp / 'unixnu.toml'
    dst = tmp / 'unixnu-new.toml'

    if run(['mcopy', '-o', '-i', str(img), '::/EFI/UniXNU/unixnu.toml', str(src)]).returncode != 0:
        return 1
    text = src.read_text(encoding='utf-8', errors='replace')
    print('---- 原 toml ----')
    print(text)

    # 先删掉所有 console 行和重复的 [ui] 段，再统一插一个
    lines = [l for l in text.splitlines() if not l.strip().startswith('console')]
    out = []
    seen_ui = False
    inserted = False
    for line in lines:
        s = line.strip()
        if s.startswith('[ui]'):
            if seen_ui:
                continue          # 丢弃重复的 [ui]
            seen_ui = True
            out.append(line)
            out.append(f'console = "{mode}"')
            inserted = True
            continue
        out.append(line)
    if not inserted:
        if out and out[-1].strip():
            out.append('')
        out.append('[ui]')
        out.append(f'console = "{mode}"')

    new = '\n'.join(out) + '\n'
    dst.write_text(new, encoding='utf-8')
    print('---- 新 toml ----')
    print(new)

    if run(['mcopy', '-o', '-i', str(img), str(dst), '::/EFI/UniXNU/unixnu.toml']).returncode != 0:
        return 1
    run(['mdir', '-i', str(img), '::/EFI/UniXNU'])
    print('完成')
    return 0


if __name__ == '__main__':
    sys.exit(main())
