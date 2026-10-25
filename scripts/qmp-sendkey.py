#!/usr/bin/env python3
"""通过 QMP 给 QEMU 发一个按键。

用法: python3 qmp-sendkey.py <qmp-socket> [键名]
  默认键名 ret（回车），也可用 spc / up / down / kp_enter 等 QEMU 键名。

用于 UniXNU 菜单：有输入设备时它会等待按键（timeout -1）。
"""
import json
import socket
import sys
import time


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    path = sys.argv[1]
    key = sys.argv[2] if len(sys.argv) > 2 else 'ret'

    for _ in range(20):
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(3)
            s.connect(path)
            break
        except OSError:
            time.sleep(0.5)
    else:
        print('!! 连不上 QMP')
        return 1

    f = s.makefile('rwb')

    def cmd(obj):
        f.write((json.dumps(obj) + '\n').encode())
        f.flush()
        try:
            return json.loads(f.readline().decode(errors='replace'))
        except Exception as e:
            return {'error': str(e)}

    cmd({'execute': 'qmp_capabilities'})
    # 先按一次，稍等再按一次，确保菜单收到
    print(cmd({'execute': 'human-monitor-command',
               'arguments': {'command-line': f'sendkey {key}'}}))
    time.sleep(0.4)
    print(cmd({'execute': 'human-monitor-command',
               'arguments': {'command-line': f'sendkey {key}'}}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
