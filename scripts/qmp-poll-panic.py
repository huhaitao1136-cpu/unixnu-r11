#!/usr/bin/env python3
"""不用 gdb：用 QMP 高频轮询 CPU 寄存器，抓进入 _panic 前的 x0。

原理：panic() 的 x0 = 格式串指针。我们每轮读 PC 和 x0..x3，
一旦发现 PC 进入 _panic / panic_trap_to_debugger 范围，
就回看上一轮记录（那时 x0 还是 panic 的参数）。

用法:
  python3 qmp-poll-panic.py <qmp socket> [轮询间隔秒]
"""
import json
import socket
import sys
import time

SOCK = sys.argv[1] if len(sys.argv) > 1 else '/tmp/poll-qmp.sock'
INTERVAL = float(sys.argv[2]) if len(sys.argv) > 2 else 0.005

# 运行期 pa（由 DarwinBoot 的 va/pa 与 macho slide 推出）
PANIC_PA = 0x489ECAAC           # _panic
PTD_PA_LO = 0x482E90D0          # _panic_trap_to_debugger
PTD_PA_HI = PTD_PA_LO + 0x800
SPIN_PA_LO = 0x482E95F4         # _panic_spin_forever


def parse_regs(text):
    """从 QEMU 'info registers' 输出里取出 PC 和 X0..X3。"""
    regs = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith('PC='):
            parts = line.split()
            for p in parts:
                if '=' in p:
                    k, v = p.split('=', 1)
                    if k in ('PC', 'X00', 'X01', 'X02', 'X03', 'X30', 'SP'):
                        try:
                            regs[k] = int(v, 16)
                        except ValueError:
                            pass
    return regs


class Qmp:
    def __init__(self, path):
        self.s = socket.socket(socket.AF_UNIX)
        self.s.connect(path)
        self.s.recv(65536)
        self.s.sendall(json.dumps({"execute": "qmp_capabilities"}).encode() + b'\n')
        self.s.recv(65536)

    def monitor(self, line):
        self.s.sendall(json.dumps({
            "execute": "human-monitor-command",
            "arguments": {"command-line": line},
        }).encode() + b'\n')
        buf = b''
        while b'"return"' not in buf:
            chunk = self.s.recv(1 << 20)
            if not chunk:
                break
            buf += chunk
        return json.loads(buf[buf.index(b'{'):].decode(errors='replace'))['return']


def main():
    q = Qmp(SOCK)
    last = None
    print('开始轮询（Ctrl-C 结束）...')
    deadline = time.time() + 60
    while time.time() < deadline:
        txt = q.monitor('info registers')
        r = parse_regs(txt)
        pc = r.get('PC')
        if pc is None:
            break
        if PANIC_PA <= pc < PANIC_PA + 0x10 or PTD_PA_LO <= pc < PTD_PA_HI or SPIN_PA_LO <= pc < SPIN_PA_LO + 0x10:
            print('===== 抓到进入 panic =====')
            print(f'当前 PC = 0x{pc:x}')
            print(f'当前 x0 = 0x{r.get("X00", 0):x}  x1 = 0x{r.get("X01", 0):x}')
            if last:
                print(f'上一轮 PC = 0x{last.get("PC", 0):x}')
                print(f'上一轮 x0 = 0x{last.get("X00", 0):x}   ← 很可能就是 panic 格式串的地址')
                print(f'上一轮 x1 = 0x{last.get("X01", 0):x}')
                print(f'上一轮 x2 = 0x{last.get("X02", 0):x}')
                print(f'上一轮 x3 = 0x{last.get("X03", 0):x}')
                print(f'上一轮 LR   = 0x{last.get("X30", 0):x}   ← 调用者')
            break
        last = r
        time.sleep(INTERVAL)
    else:
        print('60 秒内没抓到 panic（可能内核还没跑到 / 已停在别处）')
        if last:
            print(f'最后 PC = 0x{last.get("PC", 0):x}')


if __name__ == '__main__':
    main()
