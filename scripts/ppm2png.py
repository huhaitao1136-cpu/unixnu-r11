#!/usr/bin/env python3
"""把 QEMU 的 PPM(P6) 截图转成 PNG，只用标准库。

用法: python3 ppm2png.py <in.ppm> <out.png> [缩放倍数]
"""
import struct
import sys
import zlib
from pathlib import Path


def read_ppm(path: Path):
    data = path.read_bytes()
    if not data.startswith(b'P6'):
        raise SystemExit('不是 P6 PPM')
    pos = 2
    fields = []
    while len(fields) < 3:
        while pos < len(data) and data[pos:pos + 1].isspace():
            pos += 1
        if data[pos:pos + 1] == b'#':
            while pos < len(data) and data[pos] != 0x0A:
                pos += 1
            continue
        start = pos
        while pos < len(data) and not data[pos:pos + 1].isspace():
            pos += 1
        fields.append(int(data[start:pos]))
    pos += 1
    w, h, maxv = fields
    if maxv != 255:
        raise SystemExit(f'不支持的 maxval={maxv}')
    return w, h, data[pos:pos + w * h * 3]


def write_png(path: Path, w: int, h: int, rgb: bytes, scale: int = 1):
    if scale > 1:
        out = bytearray()
        for y in range(h):
            row = rgb[y * w * 3:(y + 1) * w * 3]
            wide = bytearray()
            for x in range(w):
                px = row[x * 3:x * 3 + 3]
                wide += px * scale
            for _ in range(scale):
                out += wide
        rgb = bytes(out)
        w *= scale
        h *= scale

    raw = bytearray()
    stride = w * 3
    for y in range(h):
        raw.append(0)
        raw += rgb[y * stride:(y + 1) * stride]

    def chunk(tag: bytes, payload: bytes):
        return (struct.pack('>I', len(payload)) + tag + payload +
                struct.pack('>I', zlib.crc32(tag + payload) & 0xFFFFFFFF))

    png = b'\x89PNG\r\n\x1a\n'
    png += chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0))
    png += chunk(b'IDAT', zlib.compress(bytes(raw), 6))
    png += chunk(b'IEND', b'')
    path.write_bytes(png)


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    scale = int(sys.argv[3]) if len(sys.argv) > 3 else 1
    w, h, rgb = read_ppm(src)
    write_png(dst, w, h, rgb, scale)
    print(f'{dst}  {w}x{h} 缩放x{scale}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
