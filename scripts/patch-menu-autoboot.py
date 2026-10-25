#!/usr/bin/env python3
"""让 UniXNU 菜单忽略输入设备，始终按 fallback_timeout 自动启动。

原因：Menu.c 里 HasInput = (CountInputDevices() > 0)，一旦有输入设备
      timeout 就变成 -1（永远等按键）。QEMU/AAVMF 把串口也注册成输入设备，
      所以在 QEMU 里永远卡在菜单。

改法：把 HasInput 的计算改成 FALSE（真机本来就没有可用按键设备，行为不变）。

用法:
  python3 patch-menu-autoboot.py [/root/src/UniXNU/package/UniXNUPkg/Library/UniXNULib/Menu.c]
"""
import sys
from pathlib import Path

DEFAULT = Path('/root/src/UniXNU/package/UniXNUPkg/Library/UniXNULib/Menu.c')
MARK = 'R11_AUTOBOOT'


def main() -> int:
    p = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT
    if not p.is_file():
        print(f'!! 找不到 {p}')
        return 1
    s = p.read_text(encoding='utf-8', errors='surrogateescape')
    if MARK in s:
        print('已经打过补丁')
        return 0
    old = 'HasInput = (CountInputDevices () > 0);'
    if old not in s:
        print('!! 找不到 HasInput 赋值行，请检查上游是否改过')
        return 1
    new = ('/* R11_AUTOBOOT: 忽略输入设备，始终按 fallback_timeout 自动启动。\n'
           '   真机没有可用的 ConIn 设备时行为相同；QEMU 里串口会被当作输入设备，\n'
           '   如果不强制，菜单会永远停在 -1 ms 等按键。 */\n'
           '  HasInput = FALSE;\n'
           '  (VOID)CountInputDevices;')
    s = s.replace(old, new, 1)
    p.write_text(s, encoding='utf-8', errors='surrogateescape')
    print(f'已修改 {p}')
    for i, line in enumerate(s.splitlines(), 1):
        if MARK in line or 'HasInput = ' in line:
            print(f'{i:5}: {line.strip()}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
