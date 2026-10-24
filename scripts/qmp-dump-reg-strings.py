#!/usr/bin/env python3
"""在 panic 自旋时，把 CPU 所有寄存器里"看起来像内核地址"的值都当字符串读一遍。

不打断内核、不打补丁——只靠 QMP。

用法:
  python3 qmp-dump-reg-strings.py <qmp socket>
"""
import json
import re
import socket
import sys

SOCK = sys.argv[1] if len(sys.argv) > 1 else '/tmp/poll-qmp.sock'

TEXT_VMADDR = 0xFFFFFFF007004000
LOADED_PA = 0x4807C000
DELTA = TEXT_VMADDR - LOADED_PA      # va -> pa

REG = re.compile(r'\b(X\d{2}|PC|SP|LR)=([0-9a-fA-F]+)')


def pa(va):
    return (va - DELTA) & 0xFFFFFFFFFFFFFFFF


class Qmp:
    def __init__(self, path):
        self.s = socket.socket(socket.AF_UNIX)
        self.s.connect(path)
        self.s.recv(65536)
        self.s.sendall(json.dumps({"execute": "qmp_capabilities"}).encode() + b'\n')
        self.s.recv(65536)

    def mon(self, line):
        self.s.sendall(json.dumps({
            "execute": "human-monitor-command",
            "arguments": {"command-line": line},
        }).encode() + b'\n')
        buf = b''
        while b'"return"' not in buf:
            ch = self.s.recv(1 << 20)
            if not ch:
                break
            buf += ch
        return json.loads(buf[buf.index(b'{'):].decode(errors='replace'))['return']

    def read_bytes(self, addr, n):
        out = bytearray()
        for off in range(0, n, 64):
            k = min(64, n - off)
            txt = self.mon('xp /%dxb 0x%x' % (k, addr + off))
            for tok in txt.split(':', 1)[1].replace('\n', ' ').split():
                try:
                    out.append(int(tok, 16))
                except ValueError:
                    pass
        return bytes(out)


def printable(raw):
    s = ''.join(c if 32 <= ord(c) < 127 else '.' for c in raw.decode('latin-1'))
    return s.replace('....', '.').strip('.')


def main():
    q = Qmp(SOCK)
    regs = {}
    for _ in range(3):
        txt = q.mon('info registers')
        for name, val in REG.findall(txt):
            regs[name] = int(val, 16)

    print('=== 寄存器快照 ===')
    cands = []
    for name in sorted(regs):
        v = regs[name]
        tag = ''
        if 0xFFFFFFF000000000 <= v <= 0xFFFFFFFFFFFFFFFF:
            tag = '  <- 内核地址'
            cands.append((name, v))
        print(f'  {name:4s} = 0x{v:016x}{tag}')

    print('\n=== 每个内核地址处的字符串（前 96 字节） ===')
    seen = set()
    for name, v in cands:
        for off in (0,):
            addr = pa(v + off)
            raw = q.read_bytes(addr, 96)
            hexs = raw[:32].hex()
            asc = ''.join(chr(b) if 32 <= b < 127 else '.' for b in raw)
            print(f'  [{name}=0x{v:016x}]')
            print(f'      ascii: {asc}')
            print(f'      hex  : {hexs}')


if __name__ == '__main__':
    main()
