#!/usr/bin/env python3
"""通过 QMP 从运行中的 QEMU 内核里读出 _debug_buf（含 panic 消息）。

关键：内核是被 DarwinBoot **滑动加载**的：
  macho 里 __TEXT vmaddr = 0xFFFFFFF007004000
  实际加载 va_base      = 0xFFFFFFF00427C000
  slide = vmaddr - va_base = 0x2D88000
所以符号地址要 **减去 slide** 才是运行期 va，再减 (va_base - pa_base) 得到 pa。

用法：
  python3 read-debug-buf.py [/tmp/qmp4.sock]
"""
import json
import socket
import sys

SOCK = sys.argv[1] if len(sys.argv) > 1 else '/tmp/qmp4.sock'

# 实测：QEMU info registers 的 PC = 0xFFFFFFF0072713CC
#       = _panic_trap_to_debugger(0xFFFFFFF0072710D0) + 0x2FC
# 说明运行期虚拟地址 == 符号表地址，slide = 0。
TEXT_VMADDR = 0xFFFFFFF007004000     # macho 的 __TEXT vmaddr
LOADED_PA = 0x4807C000               # DarwinBoot 打印的 pa（对应 __TEXT 起点）
SLIDE = 0
DELTA = TEXT_VMADDR - LOADED_PA      # 运行期 va -> pa

SYM_BASE = 0xFFFFFFF007A74668        # _debug_buf_base (char *)
SYM_SIZE = 0xFFFFFFF007A74678        # _debug_buf_size (UINTN)
SYM_PTR = 0xFFFFFFF007A74670         # _debug_buf_ptr
SYM_PANIC_INFO = 0xFFFFFFF007B098E8


def pa(sym_addr: int) -> int:
    return ((sym_addr - SLIDE) - DELTA) & 0xFFFFFFFFFFFFFFFF


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

    def rd64(self, sym_addr):
        out = self.monitor('xp /1gx 0x%x' % pa(sym_addr))
        return int(out.split(':', 1)[1].split()[0], 16)

    def rd_bytes(self, addr, count):
        data = bytearray()
        for off in range(0, count, 128):
            n = min(128, count - off)
            out = self.monitor('xp /%dxb 0x%x' % (n, addr + off))
            for tok in out.split(':', 1)[1].replace('\n', ' ').split():
                try:
                    data.append(int(tok, 16))
                except ValueError:
                    pass
        return bytes(data)


def printable(raw):
    return ''.join(c if (32 <= ord(c) < 127 or c in '\r\n\t') else '.' for c in raw.decode('latin-1'))


def main():
    print(f'slide=0x{SLIDE:x}  va->pa delta=0x{DELTA:x}')
    q = Qmp(SOCK)

    base = q.rd64(SYM_BASE)
    size = q.rd64(SYM_SIZE)
    ptr = q.rd64(SYM_PTR)
    print('_debug_buf_base = 0x%x' % base)
    print('_debug_buf_ptr  = 0x%x' % ptr)
    print('_debug_buf_size = 0x%x' % size)

    # base 是运行期 va（已滑动）
    if 0xFFFFFFF000000000 <= base <= 0xFFFFFFFFFFFFFFFF and 0 < size < (1 << 24):
        n = min(size, 8192)
        raw = q.rd_bytes((base - DELTA) & 0xFFFFFFFFFFFFFFFF, n)
        print('---- debug buffer (%d 字节) ----' % n)
        print(printable(raw))
    else:
        print('debug_buf 仍为 0，改读 _panic_info：')
        for i in range(6):
            print(q.monitor('xp /2gx 0x%x' % (pa(SYM_PANIC_INFO) + i * 16)).strip())


if __name__ == '__main__':
    main()
