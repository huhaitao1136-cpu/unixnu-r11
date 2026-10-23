#!/usr/bin/env python3
"""extract-fv.py — recursively unpack Qualcomm bootloader images (xbl/abl/tz/mdtp)
into FV/FFS/sections and export the PE32/TE images, i.e. the vendor DXEs that
Mu-Silicium expects under Binaries/<device>/QcomPkg/Drivers/.

Chain: FV -> FFS file -> SECTION (COMPRESSION / GUID_DEFINED / FV_IMAGE) -> PE32.

Usage:
    python extract-fv.py R11-backup/firmware/abl.img -o R11-backup/extract/abl --efi
    python extract-fv.py R11-backup/firmware/xbl.img -o R11-backup/extract/xbl --efi
"""
from __future__ import annotations

import argparse
import gzip
import lzma
import struct
import sys
import uuid
import zlib
from pathlib import Path

FV_SIG = b"_FVH"
GUID_LZMA = uuid.UUID("ee4e5898-3914-4259-9d6e-dc7bd79403cf")
GUID_CRC32 = uuid.UUID("fc1bcdb0-7d31-49aa-936a-a4600d9dd083")

SEC = {0x01: "COMPRESSION", 0x02: "GUID_DEFINED", 0x03: "DISPOSABLE", 0x10: "PE32",
       0x11: "PIC", 0x12: "TE", 0x13: "DXE_DEPEX", 0x14: "VERSION", 0x15: "USER_INTERFACE",
       0x16: "COMPAT16", 0x17: "FV_IMAGE", 0x18: "FREEFORM", 0x19: "RAW", 0x1B: "PEI_DEPEX",
       0x1C: "SMM_DEPEX"}

FTYPE = {0x01: "RAW", 0x02: "FREEFORM", 0x03: "SEC", 0x04: "PEI_CORE", 0x05: "PEIM",
         0x06: "DXE_CORE", 0x07: "DXE_DRIVER", 0x08: "PEIM_DXE", 0x09: "APPLICATION",
         0x0A: "SMM", 0x0B: "FV_IMAGE", 0x0C: "COMBINED_PEIM", 0x0D: "SMM_CORE",
         0x0E: "PAD", 0xF0: "FFS_PAD"}


def u16(b, o): return struct.unpack_from("<H", b, o)[0]
def u24(b, o): return b[o] | (b[o + 1] << 8) | (b[o + 2] << 16)
def u32(b, o): return struct.unpack_from("<I", b, o)[0]
def u64(b, o): return struct.unpack_from("<Q", b, o)[0]


def is_fv(buf: bytes, off: int = 0) -> bool:
    return len(buf) >= off + 0x48 and buf[off + 40:off + 44] == FV_SIG


def lzma_try(data: bytes) -> bytes | None:
    for skip in (0, 1, 2, 3, 4, 5):
        blob = data[skip:]
        if len(blob) < 13:
            continue
        props = blob[0]
        lc = props % 9
        p = props // 9
        lp = p % 5
        pb = p // 5
        if lc > 4 or lp > 4 or pb > 4:
            continue
        dict_size = u32(blob, 1)
        usize = u64(blob, 5)
        if not (0x1000 <= dict_size <= 0x40000000) or usize > 0x40000000:
            continue
        try:
            d = lzma.LZMADecompressor(
                format=lzma.FORMAT_RAW,
                filters=[{"id": lzma.FILTER_LZMA1, "lc": lc, "lp": lp, "pb": pb,
                          "dict_size": max(dict_size, 1 << 12)}])
            out = d.decompress(blob[13:])
            if out and len(out) > 16:
                return out
        except Exception:
            continue
    return None


def gzip_try(data: bytes) -> bytes | None:
    for skip in (0, 1, 2, 3):
        if data[skip:skip + 2] != b"\x1f\x8b":
            continue
        try:
            return zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(data[skip:])
        except Exception:
            try:
                return gzip.decompress(data[skip:])
            except Exception:
                continue
    return None


class Ctx:
    def __init__(self, outdir: Path, save: bool):
        self.outdir = outdir
        self.save = save
        self.depth = 0
        self.items: list[dict] = []
        self.saved = 0

    def emit(self, name: str, kind: str, body: bytes, guid: str = ""):
        self.items.append({"name": name, "kind": kind, "size": len(body), "guid": guid})
        print("  " * self.depth + f"[{kind:9}] {len(body):>8} B  {name}")
        if self.save and kind in ("PE32", "TE", "PIC"):
            safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in name)[:64] or "unnamed"
            p = self.outdir / f"{safe}.efi"
            n = 1
            while p.exists():
                p = self.outdir / f"{safe}_{n}.efi"
                n += 1
            p.write_bytes(body)
            self.saved += 1
        elif self.save and kind in ("FREEFORM", "RAW") and "." in name:
            safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in name)[:64]
            raw = self.outdir / "raw"
            raw.mkdir(exist_ok=True)
            (raw / safe).write_bytes(body)


def walk_sections(buf: bytes, start: int, end: int, name: str, ctx: Ctx, guid: str = ""):
    o = start
    while o + 4 <= end:
        size = u24(buf, o)
        stype = buf[o + 3]
        if size < 4 or o + size > end:
            return
        body = buf[o + 4:o + size]
        if stype in (0x10, 0x11, 0x12):
            ctx.emit(name, SEC[stype], body, guid)
        elif stype == 0x02:
            g = uuid.UUID(bytes_le=bytes(body[:16]))
            data_off = u16(body, 16)
            pstart = o + (data_off if data_off >= 4 else 4 + data_off)
            payload = buf[pstart:o + size]
            if g == GUID_LZMA:
                inner = lzma_try(payload)
            elif g == GUID_CRC32:
                inner = payload
            else:
                inner = gzip_try(payload) or lzma_try(payload)
            if inner is None:
                print("  " * ctx.depth + f"  (GUID_DEFINED {g} decompress failed, {len(payload)} B)")
            elif is_fv(inner):
                walk_fv(inner, name, ctx)
            else:
                walk_sections(inner, 0, len(inner), name, ctx, guid)
        elif stype == 0x01:
            ctype = body[4] if len(body) > 4 else 0
            if ctype == 0:
                walk_sections(body, 5, len(body), name, ctx)
            else:
                print("  " * ctx.depth + f"  (COMPRESSION type={ctype} unsupported, unclen=0x{u32(body, 0):x})")
        elif stype == 0x17:
            if is_fv(body):
                walk_fv(body, name, ctx)
            else:
                inner = gzip_try(body) or lzma_try(body)
                if inner and is_fv(inner):
                    walk_fv(inner, name, ctx)
                else:
                    print("  " * ctx.depth + f"  (FV_IMAGE unparsable, {len(body)} B, head={body[:12].hex(' ')})")
        o += (size + 3) & ~3


def walk_fv(buf: bytes, label: str, ctx: Ctx):
    ctx.depth += 1
    fv_len = u64(buf, 32)
    if fv_len <= 0 or fv_len > len(buf):
        fv_len = len(buf)
    hdr_len = u16(buf, 48) or 0x48
    print("  " * ctx.depth + f"FV {label} len=0x{fv_len:x} hdr=0x{hdr_len:x}")
    o = hdr_len
    while o + 24 <= fv_len:
        size = u24(buf, o + 20)
        ftype = buf[o + 18]
        if size < 24 or o + size > fv_len:
            break
        body = buf[o + 24:o + size]
        ui = ""
        p = 0
        while p + 4 <= len(body):
            s = u24(body, p)
            t = body[p + 3]
            if s < 4 or p + s > len(body):
                break
            if t == 0x15:
                try:
                    ui = body[p + 4:p + s].decode("utf-16-le").rstrip("\0")
                except Exception:
                    pass
                break
            p += (s + 3) & ~3
        fname = ui or f"ffs-{o:06x}"
        fguid = str(uuid.UUID(bytes_le=bytes(buf[o:o + 16])))
        print("  " * ctx.depth + f"- {FTYPE.get(ftype, hex(ftype)):12} {size:>8} B  {fname}")
        walk_sections(body, 0, len(body), fname, ctx, fguid)
        # RAW / FREEFORM 文件往往没有 section 头，直接按文件名落盘
        if ctx.save and ftype in (0x01, 0x02) and "." in fname:
            raw = ctx.outdir / "raw"
            raw.mkdir(exist_ok=True)
            safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in fname)[:64]
            (raw / safe).write_bytes(body)
        o += (size + 7) & ~7
    ctx.depth -= 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("-o", "--outdir", default="fv-out")
    ap.add_argument("--efi", action="store_true")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()

    buf = Path(args.image).read_bytes()
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    ctx = Ctx(outdir, args.efi)

    print(f"### {Path(args.image).name} ({len(buf)} B)")
    cand = [i - 0x28 for i in range(0x28, len(buf) - 4) if buf[i:i + 4] == FV_SIG]
    roots: list[int] = []
    for s in cand:
        if s < 0:
            continue
        if not roots or s - roots[-1] > 0x2000:
            roots.append(s)
    if not args.all:
        roots = roots[:1]
    for s in roots:
        walk_fv(buf[s:], f"@0x{s:x}", ctx)

    (outdir / "index.txt").write_text(
        "\n".join(f"{i['kind']}\t{i['size']}\t{i['name']}\t{i.get('guid','')}" for i in ctx.items), encoding="utf-8")
    print(f"\n{len(ctx.items)} section(s), {ctx.saved} exported -> {outdir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
