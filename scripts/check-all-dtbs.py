#!/usr/bin/env python3
"""把 boot.img 里所有 FDT 都提取出来，逐个反编译并检查按键节点。

用法:
  python3 check-all-dtbs.py <boot.img> [输出目录]
"""
import struct
import subprocess
import sys
from pathlib import Path

FDT_MAGIC = b'\xd0\x0d\xfe\xed'


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    img = Path(sys.argv[1])
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else Path('/tmp/r11dtbs')
    out.mkdir(parents=True, exist_ok=True)

    d = img.read_bytes()
    offs = []
    i = 0
    while True:
        i = d.find(FDT_MAGIC, i)
        if i < 0:
            break
        size = struct.unpack_from('>I', d, i + 4)[0]
        if 0x1000 < size < 0x200000:
            offs.append((i, size))
        i += 4

    print(f'找到 {len(offs)} 个 FDT')
    for n, (off, size) in enumerate(offs, 1):
        dtb = out / f'fdt{n}.dtb'
        dts = out / f'fdt{n}.dts'
        dtb.write_bytes(d[off:off + size])
        r = subprocess.run(['dtc', '-I', 'dtb', '-O', 'dts', '-o', str(dts), str(dtb)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print(f'  fdt{n}: 反编译失败 {r.stderr[:80]}')
            continue
        text = dts.read_text(errors='replace')
        # 找 gpio_keys 节点里的 gpios
        keys = []
        for line in text.splitlines():
            s = line.strip()
            if ('vol_up' in s or 'vol_down' in s or 'gpios = <' in s
                    or 'linux,code' in s or 'label = "volume' in s):
                keys.append(s)
        # 找 tlmm 基址
        tlmm = [l.strip() for l in text.splitlines() if 'pinctrl@' in l]
        print(f'  fdt{n}: {size} B, 按键行 {len(keys)} 条')
        for k in keys[:8]:
            print(f'      {k}')
        for t in tlmm[:3]:
            print(f'      TLMM: {t}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
