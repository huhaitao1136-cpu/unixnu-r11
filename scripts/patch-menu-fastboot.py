#!/usr/bin/env python3
"""让 UniXNU 菜单立刻启动默认项（把倒计时固定成 1.5 秒）。

配合 patch-menu-autoboot.py 使用：那个解决"有输入设备就永远等"，
这个解决"菜单循环重进导致倒计时反复重置"。

用法:
  python3 patch-menu-fastboot.py [/root/src/UniXNU/package/UniXNUPkg/Library/UniXNULib/Menu.c]
"""
import sys
from pathlib import Path

DEFAULT = Path('/root/src/UniXNU/package/UniXNUPkg/Library/UniXNULib/Menu.c')
MARK = 'R11_FASTBOOT'


def main() -> int:
    p = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT
    if not p.is_file():
        print(f'!! 找不到 {p}')
        return 1
    lines = p.read_text(encoding='utf-8', errors='surrogateescape').splitlines(keepends=True)
    if any(MARK in l for l in lines):
        print('已经打过补丁')
        return 0

    changed = 0
    out = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith('RemainingMs =') and ';' in stripped:
            indent = line[:len(line) - len(line.lstrip())]
            out.append(f'{indent}RemainingMs = 1500; /* {MARK} */\n')
            changed += 1
        else:
            out.append(line)
    if not changed:
        print('!! 没找到 RemainingMs 赋值行')
        return 1
    p.write_text(''.join(out), encoding='utf-8', errors='surrogateescape')
    print(f'已把 {changed} 处倒计时固定为 1500 ms')
    for i, line in enumerate(''.join(out).splitlines(), 1):
        if MARK in line:
            print(f'{i:5}: {line.strip()}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
