#!/usr/bin/env python3
"""把 QEMU 的串口接成"可读可写"的桥：收字节写日志文件，定期发送回车。

AAVMF 把串口注册成 ConIn，所以往串口写 \\r 就等于在 UniXNU 菜单里按回车；
同时把串口输出持续写进日志文件（qemu 不再用 -serial file:）。

用法:
  python3 serial-bridge.py <socket路径> <日志文件> [发送回车次数] [间隔秒]
"""
import os
import socket
import sys
import threading
import time


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    path, logpath = sys.argv[1], sys.argv[2]
    presses = int(sys.argv[3]) if len(sys.argv) > 3 else 3
    interval = float(sys.argv[4]) if len(sys.argv) > 4 else 3.0

    for _ in range(40):
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(5)
            s.connect(path)
            break
        except OSError:
            time.sleep(0.5)
    else:
        print('!! 连不上串口 socket')
        return 1

    log = open(logpath, 'ab', buffering=0)

    def reader():
        while True:
            try:
                data = s.recv(65536)
            except OSError:
                break
            if not data:
                break
            log.write(data)
        log.close()

    t = threading.Thread(target=reader, daemon=True)
    t.start()

    time.sleep(4)
    for _ in range(presses):
        try:
            s.sendall(b'\r')
        except OSError:
            break
        time.sleep(interval)
    return 0


if __name__ == '__main__':
    sys.exit(main())
