#!/usr/bin/env python3
"""把某个符号入口改成 `mov w0,#0; ret`。

用法: python3 patch-kernel-symbol.py <kernel.macho> <符号名> [返回值]
例如: python3 patch-kernel-symbol.py /tmp/kp.img _load_code_signature
"""
import struct
import subprocess
import sys


def main(argv):
    if len(argv) < 3:
        raise SystemExit(__doc__)
    path, name = argv[1], argv[2]
    ret = int(argv[3], 0) if len(argv) > 3 else 0

    nm = subprocess.run(['llvm-nm', path], capture_output=True, text=True).stdout
    addr = None
    for line in nm.splitlines():
        f = line.split()
        if len(f) == 3 and f[2] == name:
            addr = int(f[0], 16)
            break
    if addr is None:
        raise SystemExit('找不到符号 %s' % name)
    print('符号 %s = 0x%x' % (name, addr))

    data = bytearray(open(path, 'rb').read())
    ncmds = struct.unpack_from('<I', data, 16)[0]
    off = 32
    off_file = None
    for _ in range(ncmds):
        cmd, sz = struct.unpack_from('<II', data, off)
        if cmd == 0x19:
            vm, vs, fo, fs = struct.unpack_from('<QQQQ', data, off + 24)
            if vm <= addr < vm + vs:
                off_file = fo + (addr - vm)
        off += sz
    if off_file is None:
        raise SystemExit('符号不在任何段里')

    before = data[off_file:off_file + 8].hex()
    # mov w0, #imm   (imm 0..0xffff)  -> MOVZ W0
    data[off_file:off_file + 4] = struct.pack('<I', 0x52800000 | ((ret & 0xFFFF) << 5))
    data[off_file + 4:off_file + 8] = struct.pack('<I', 0xD65F03C0)
    open(path, 'wb').write(data)
    print('文件偏移 0x%x：%s -> %s' % (off_file, before, data[off_file:off_file + 8].hex()))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
