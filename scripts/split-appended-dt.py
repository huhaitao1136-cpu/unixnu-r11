#!/usr/bin/env python3
"""split-appended-dt.py — 从 msm 风格 boot.img 里拆出 gzip 内核 + 追加的多个 DTB。

msm-4.4 的 boot.img 惯例：header 的 kernel 字段 = gzip(Image) + 一串连续 FDT
（没有 QCDT 表时，每个 FDT 自带 totalsize，可以顺序切）。本脚本：
  1. 用 decompressobj 找 gzip 成员真实结尾（gzip.decompress 遇到尾部非 gzip 数据会报错）
  2. 从尾部顺序切出所有 FDT，打印 model / compatible / board-id
  3. 把每个 DTB 写成文件，并把命中的那个 dump 成 dts 文本

用法：
    python split-appended-dt.py R11-backup/firmware/ota-boot.img -o inputs/dt
"""
from __future__ import annotations

import argparse
import struct
import sys
import zlib
from pathlib import Path

FDT_MAGIC = 0xD00DFEED


class FDT:
    def __init__(self, blob: bytes):
        (magic, self.total, off_struct, off_strings, _off_rsv,
         self.version, self.last_comp, _cpu, size_strings, size_struct) = struct.unpack_from(">10I", blob, 0)
        if magic != FDT_MAGIC:
            raise ValueError("bad magic")
        self.blob = blob[:self.total]
        self.strings = blob[off_strings:off_strings + size_strings]
        self.struct = blob[off_struct:off_struct + size_struct]

    def walk(self):
        i = 0
        depth = 0
        stack: list[str] = []
        while i + 4 <= len(self.struct):
            tok = struct.unpack_from(">I", self.struct, i)[0]; i += 4
            if tok == 1:  # BEGIN_NODE
                end = self.struct.find(b"\0", i)
                if end < 0:
                    break
                name = self.struct[i:end].decode(errors="replace")
                i = (end + 4) & ~3
                stack.append(name or "/")
                yield depth, stack[-1], None, stack[:]
                depth += 1
            elif tok == 2:  # END_NODE
                depth -= 1
                if stack:
                    stack.pop()
                if depth < 0:
                    break
            elif tok == 3:  # PROP
                ln, nameoff = struct.unpack_from(">II", self.struct, i); i += 8
                val = self.struct[i:i + ln]
                i = (i + ln + 3) & ~3
                e = self.strings.find(b"\0", nameoff)
                pname = self.strings[nameoff:e].decode(errors="replace")
                yield depth - 1, None, (pname, val), stack[:]
            elif tok == 4:
                continue
            elif tok == 9:
                break

    def dump(self) -> str:
        out = []
        for depth, name, prop, _p in self.walk():
            if name is not None:
                out.append("  " * depth + name + " {")
            elif prop:
                out.append("  " * (depth + 1) + f'{prop[0]} = {self.fmt(prop[1])!r};')
        return "\n".join(out)

    def find(self, needle: str) -> list[tuple[list[str], str, bytes]]:
        out = []
        for _d, name, prop, path in self.walk():
            if prop and needle in prop[0]:
                out.append((path, prop[0], prop[1]))
            elif name and needle in name:
                out.append((path, "<node>", b""))
        return out

    @staticmethod
    def fmt(val: bytes, maxv: int = 96) -> str:
        if not val:
            return "<empty>"
        if all(32 <= b < 127 for b in val) and len(val) > 1:
            return val.rstrip(b"\0").decode(errors="replace")
        if len(val) <= 32 and len(val) % 4 == 0 and len(val) > 4:
            return "<" + " ".join(f"0x{struct.unpack_from('>I', val, k)[0]:x}" for k in range(0, len(val), 4)) + ">"
        if len(val) == 4:
            return f"<0x{struct.unpack('>I', val)[0]:x}>"
        if len(val) == 8:
            return f"<0x{struct.unpack('>Q', val)[0]:x}>"
        return f"<{len(val)}B " + " ".join(f"{b:02x}" for b in val[:maxv]) + (" ..." if len(val) > maxv else "") + ">"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("-o", "--outdir", default="inputs/dt")
    args = ap.parse_args()

    buf = Path(args.image).read_bytes()
    if buf[:8] != b"ANDROID!":
        raise SystemExit("not an Android boot image")
    kernel_size = struct.unpack_from("<I", buf, 8)[0]
    page = struct.unpack_from("<I", buf, 36)[0]
    koff = page
    kregion = buf[koff:koff + kernel_size]

    # 1) gzip 成员的真正结尾
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    image = d.decompress(kregion) + d.flush()
    consumed = len(kregion) - len(d.unused_data) - len(d.unconsumed_tail)
    tail = d.unused_data + d.unconsumed_tail
    print(f"gzip member: {consumed} B compressed -> {len(image)} B Image")
    print(f"appended tail: {len(tail)} B")

    # 2) 顺序切 FDT
    outdir = Path(args.outdir); outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "Image.bin").write_bytes(image)

    pos = 0
    idx = 0
    rows = []
    while pos + 40 <= len(tail):
        magic = struct.unpack_from(">I", tail, pos)[0]
        if magic != FDT_MAGIC:
            # 找下一个对齐的魔法
            nxt = tail.find(struct.pack(">I", FDT_MAGIC), pos)
            if nxt < 0:
                break
            pos = nxt
            continue
        try:
            t = FDT(tail[pos:])
        except Exception:
            pos += 4
            continue
        model = ""
        compat = ""
        board = ""
        for _d, node, prop, path in t.walk():
            if not prop:
                continue
            pname, val = prop
            if pname == "model":
                model = val.rstrip(b"\0").decode(errors="replace")
            elif pname == "compatible" and path and path[0] == "/":
                compat = " ".join(x for x in val.decode(errors="replace").split("\0") if x)
            elif pname == "qcom,board-id":
                board = " ".join(f"0x{struct.unpack_from('>I', val, k)[0]:x}" for k in range(0, len(val) - 3, 4))
            if model and compat and board:
                break
        fn = outdir / f"dtb-{idx:02d}.dtb"
        fn.write_bytes(t.blob)
        (outdir / f"dtb-{idx:02d}.dts").write_text(t.dump(), encoding="utf-8")
        rows.append((idx, pos, t.total, model, compat, board))
        print(f"  [{idx:02d}] off={pos:#09x} size={t.total:>7}  board-id={board:<24} {model}")
        print(f"        compatible: {compat}")
        pos += t.total
        idx += 1

    print(f"\ntotal {idx} DTB(s) -> {outdir}")
    (outdir / "index.txt").write_text(
        "\n".join(f"{i}\t{off:#x}\t{size}\t{model}\t{compat}\t{board}"
                  for i, off, size, model, compat, board in rows), encoding="utf-8")

    # 3) 内核版本
    import re
    m = re.search(rb"Linux version [^\0]{0,120}", image)
    print("\nkernel:", m.group(0).decode(errors="replace") if m else "(not found in Image)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
