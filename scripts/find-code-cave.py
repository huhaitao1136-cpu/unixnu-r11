#!/usr/bin/env python3
"""在 Mach-O 的 __TEXT_EXEC 里找连续 NOP（或 0）空洞，用于放 trampoline。

用法: python3 find-code-cave.py <kernel.macho> [最小字节数]
"""
import struct
import sys
from pathlib import Path

NOP = b'\x1f\x20\x03\xd5'


def segments(d: bytes):
    _, _, _, _, ncmds, _, _ = struct.unpack_from('<IiiIIII', d, 0)
    off = 32
    out = []
    for _ in range(ncmds):
        cmd, cmdsize = struct.unpack_from('<II', d, off)
        if cmd == 0x19:
            name = d[off + 8:off + 24].rstrip(b'\0').decode(errors='replace')
            vmaddr, vmsize, fileoff, filesize = struct.unpack_from('<QQQQ', d, off + 24)
            out.append((name, vmaddr, vmsize, fileoff, filesize))
        off += cmdsize
    return out


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    macho = Path(sys.argv[1])
    need = int(sys.argv[2]) if len(sys.argv) > 2 else 16
    d = macho.read_bytes()

    for name, vm, vs, fo, fs in segments(d):
        if name != '__TEXT_EXEC':
            continue
        blob = d[fo:fo + fs]
        # 找连续 NOP
        runs = []
        i = 0
        while i < len(blob) - 4:
            if blob[i:i + 4] == NOP:
                j = i
                while j < len(blob) - 4 and blob[j:j + 4] == NOP:
                    j += 4
                if (j - i) >= need:
                    runs.append((i, j - i))
                i = j
            else:
                i += 4
        print(f'__TEXT_EXEC: {len(runs)} 段 NOP 空洞 >= {need} 字节')
        for off, length in runs[:10]:
            print(f'  VA 0x{vm + off:012x}  长度 {length} 字节')
    return 0


if __name__ == '__main__':
    sys.exit(main())
