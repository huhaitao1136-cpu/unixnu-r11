#!/usr/bin/env python3
"""通过 QMP 给运行中的 QEMU 截屏（screendump）。

用法:
  python3 qmp-shot.py <qmp socket> <输出 .ppm>
"""
import json
import socket
import sys


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    sock, out = sys.argv[1], sys.argv[2]
    s = socket.socket(socket.AF_UNIX)
    s.connect(sock)
    s.recv(65536)
    s.sendall(json.dumps({"execute": "qmp_capabilities"}).encode() + b'\n')
    s.recv(65536)
    s.sendall(json.dumps({
        "execute": "screendump",
        "arguments": {"filename": out},
    }).encode() + b'\n')
    buf = b''
    while b'"return"' not in buf and b'"error"' not in buf:
        chunk = s.recv(65536)
        if not chunk:
            break
        buf += chunk
    print(buf.decode(errors='replace')[:400])
    print(f'截屏: {out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
