#!/usr/bin/env python3
"""把 kernel.macho 里 v8.0 CPU 不支持的 PAN 指令改掉。

规则：
  MSR PAN, #imm   0xD500419F         -> NOP
  MRS xN, PAN     0xD5384260|N       -> MOV wN, #1   （XNU 检查 PAN 必须为 1）
  0xE7FFDEFF（非法编码）             -> NOP

用法:
  python3 patch-pan.py <输入 kernel.macho> <输出 kernel.macho>
"""
import struct
import sys
from pathlib import Path

NOP = struct.pack('<I', 0xD503201F)
MOV_W1 = 0x52800020          # mov wN, #1  （XNU 要求 PAN 显示为启用）


def patch(src: Path, dst: Path) -> dict:
    d = bytearray(src.read_bytes())
    stat = {'msr_pan': 0, 'mrs_pan': 0, 'illegal': 0}
    for i in range(0, len(d) - 4, 4):
        w = struct.unpack_from('<I', d, i)[0]
        if w == 0xD500419F:
            d[i:i + 4] = NOP
            stat['msr_pan'] += 1
        elif (w & 0xFFFFFFE0) == 0xD5384260:
            d[i:i + 4] = struct.pack('<I', MOV_W1 | (w & 0x1F))
            stat['mrs_pan'] += 1
        elif w == 0xE7FFDEFF:
            d[i:i + 4] = NOP
            stat['illegal'] += 1
    dst.write_bytes(d)
    return stat


def verify(path: Path) -> dict:
    d = path.read_bytes()
    left = {'msr_pan': 0, 'mrs_pan': 0, 'illegal': 0}
    for i in range(0, len(d) - 4, 4):
        w = struct.unpack_from('<I', d, i)[0]
        if w == 0xD500419F:
            left['msr_pan'] += 1
        elif (w & 0xFFFFFFE0) == 0xD5384260:
            left['mrs_pan'] += 1
        elif w == 0xE7FFDEFF:
            left['illegal'] += 1
    return left


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    if not src.is_file():
        print(f'!! 找不到 {src}')
        return 1
    stat = patch(src, dst)
    print(f'替换: MSR PAN {stat["msr_pan"]} 条, MRS PAN {stat["mrs_pan"]} 条, 非法编码 {stat["illegal"]} 条')
    left = verify(dst)
    print(f'剩余: MSR PAN {left["msr_pan"]}, MRS PAN {left["mrs_pan"]}, 非法 {left["illegal"]}')
    print(f'输出: {dst} ({dst.stat().st_size} B)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
