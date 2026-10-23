#!/usr/bin/env python3
"""inspect-bootimg.py — 解析 Android boot image，导出版本参数、内核、ramdisk 和内置 FDT。

用途：给 UniXNU 的 [bootimg.image] 与 Mu-Silicium 的 [boot_image] 提供实测参数，
并把 stock 设备树里的 panel / touch / 内存布局翻出来。

用法：
    python inspect-bootimg.py R11-backup/firmware/ota-boot.img -o R11-backup/boot-out
"""
from __future__ import annotations

import argparse
import gzip
import io
import json
import re
import struct
import sys
import zlib
from pathlib import Path

FDT_MAGIC = 0xD00DFEED
FDT_BEGIN_NODE, FDT_END_NODE, FDT_PROP, FDT_NOP, FDT_END = 1, 2, 3, 4, 9


def parse_header(buf: bytes) -> dict:
    if buf[:8] != b"ANDROID!":
        raise SystemExit("not an Android boot image (no ANDROID! magic)")
    (magic, kernel_size, kernel_addr, ramdisk_size, ramdisk_addr,
     second_size, second_addr, tags_addr, page_size, header_version) = struct.unpack_from("<8s9I", buf, 0)
    hdr = {
        "magic": magic.decode(),
        "kernel_size": kernel_size, "kernel_addr": kernel_addr,
        "ramdisk_size": ramdisk_size, "ramdisk_addr": ramdisk_addr,
        "second_size": second_size, "second_addr": second_addr,
        "tags_addr": tags_addr, "page_size": page_size,
        "header_version": header_version,
        "os_version": struct.unpack_from("<I", buf, 44)[0],
    }
    hdr["name"] = buf[48:64].split(b"\0")[0].decode(errors="replace")
    hdr["cmdline"] = buf[64:64 + 512].split(b"\0")[0].decode(errors="replace")
    hdr["id"] = buf[576:608].hex()
    hdr["extra_cmdline"] = buf[608:608 + 1024].split(b"\0")[0].decode(errors="replace")
    if header_version >= 1:
        hdr["recovery_dtbo_size"] = struct.unpack_from("<I", buf, 1632)[0]
        hdr["recovery_dtbo_offset"] = struct.unpack_from("<Q", buf, 1636)[0]
        hdr["header_size"] = struct.unpack_from("<I", buf, 1644)[0]
    if header_version >= 2:
        hdr["dtb_size"] = struct.unpack_from("<I", buf, 1648)[0]
        hdr["dtb_addr"] = struct.unpack_from("<Q", buf, 1652)[0]
    return hdr


def page_align(n: int, page: int) -> int:
    return (n + page - 1) // page * page


def slices(buf: bytes, h: dict) -> dict:
    p = h["page_size"]
    off = p
    out = {}
    out["kernel"] = (off, h["kernel_size"]); off += page_align(h["kernel_size"], p)
    out["ramdisk"] = (off, h["ramdisk_size"]); off += page_align(h["ramdisk_size"], p)
    if h.get("second_size"):
        out["second"] = (off, h["second_size"]); off += page_align(h["second_size"], p)
    if h.get("recovery_dtbo_size"):
        out["recovery_dtbo"] = (off, h["recovery_dtbo_size"]); off += page_align(h["recovery_dtbo_size"], p)
    if h.get("dtb_size"):
        out["dtb"] = (off, h["dtb_size"]); off += page_align(h["dtb_size"], p)
    out["_end"] = (off, len(buf) - off)
    return out


def decompress_kernel(raw: bytes) -> tuple[bytes, str]:
    if raw[:2] == b"\x1f\x8b":
        return gzip.decompress(raw), "gzip"
    if raw[:4] == b"\x04\x22\x4d\x18":
        try:
            import lz4.block  # type: ignore
            return lz4.block.decompress(raw, uncompressed_size=64 << 20), "lz4"
        except Exception:
            return raw, "lz4 (not decompressed)"
    if raw[:6] == b"\xfd7zXZ\x00":
        import lzma
        return lzma.decompress(raw), "xz"
    if raw[:2] == b"\x02\x21" or raw[:3] == b"\x5d\x00\x00":
        return raw, "lzma (not decompressed)"
    if b"Linux version" in raw[:2 << 20]:
        return raw, "raw"
    return raw, "unknown"


class FDT:
    def __init__(self, blob: bytes):
        (magic, self.total, off_struct, off_strings, off_rsv,
         self.version, self.last_comp, self.boot_cpu, size_strings, size_struct) = struct.unpack_from(
            ">10I", blob, 0)
        if magic != FDT_MAGIC:
            raise ValueError("bad FDT magic")
        self.strings = blob[off_strings:off_strings + size_strings]
        self.struct = blob[off_struct:off_struct + size_struct]

    def s(self, off: int) -> str:
        end = self.strings.find(b"\0", off)
        return self.strings[off:end].decode(errors="replace")

    def walk(self):
        """yield (depth, name, props) — props 为 [(name, value)]"""
        i = 0
        depth = 0
        stack = []
        while i < len(self.struct):
            tok = struct.unpack_from(">I", self.struct, i)[0]; i += 4
            if tok == FDT_BEGIN_NODE:
                end = self.struct.find(b"\0", i)
                name = self.struct[i:end].decode(errors="replace")
                i = (end + 4) & ~3
                stack.append(name or "/")
                yield depth, stack[-1], None
                depth += 1
            elif tok == FDT_END_NODE:
                depth -= 1
                if stack:
                    stack.pop()
            elif tok == FDT_PROP:
                ln, nameoff = struct.unpack_from(">II", self.struct, i); i += 8
                val = self.struct[i:i + ln]
                i = (i + ln + 3) & ~3
                yield depth - 1, None, (self.s(nameoff), val)
            elif tok == FDT_NOP:
                continue
            elif tok == FDT_END:
                break

    def dump(self, max_val: int = 64) -> str:
        lines = []
        for depth, name, prop in self.walk():
            if name is not None:
                lines.append("  " * depth + name)
            else:
                pname, val = prop
                shown = self._fmt(val, max_val)
                lines.append("  " * depth + f"  {pname} = {shown}")
        return "\n".join(lines)

    @staticmethod
    def _fmt(val: bytes, max_val: int) -> str:
        if not val:
            return "<empty>"
        if all(32 <= b < 127 for b in val) and len(val) > 1:
            return repr(val.decode()).replace("\\x00", " ").replace("'", '"')
        if len(val) == 4:
            return f"<0x{struct.unpack('>I', val)[0]:x}>"
        if len(val) == 8:
            return f"<0x{struct.unpack('>Q', val)[0]:x}>"
        head = val[:max_val]
        return f"<{len(val)}B: " + " ".join(f"{b:02x}" for b in head) + (" ..." if len(val) > max_val else "") + ">"


KEYWORDS = [
    "mdss_dsi", "panel", "s6e3fa3", "oppo16051", "synaptics", "focaltech", "goodix",
    "novatek", "pm660", "qusb2", "dwc3", "sdhci", "sdm660", "kd_", "cortex-a73", "cortex-a53",
    "Linux version", "oppo", "r11",
]


def scan_strings(blob: bytes, label: str, extra: int = 12) -> None:
    print(f"\n--- strings hits: {label} ---")
    found: dict[str, list[str]] = {}
    for kw in KEYWORDS:
        hits = [m.start() for m in re.finditer(re.escape(kw.encode()), blob, re.I)][:extra]
        if hits:
            samples = []
            for h in hits[:4]:
                seg = blob[max(0, h - 24):h + 60]
                txt = "".join(chr(c) if 32 <= c < 127 else "." for c in seg)
                samples.append(txt)
            found[kw] = samples
            print(f"  [{kw}] {len(hits)}{'+' if len(hits) >= extra else ''} hits")
            for s in samples:
                print(f"      {s}")
    if not found:
        print("  (no hits)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("-o", "--outdir", default="boot-extracted")
    ap.add_argument("--dump-dtb", action="store_true", help="print the whole device tree")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    src = Path(args.image)
    buf = src.read_bytes()
    h = parse_header(buf)
    sl = slices(buf, h)
    out = Path(args.outdir); out.mkdir(parents=True, exist_ok=True)

    print(f"=== {src.name} ({len(buf)} bytes) ===")
    for k in ("magic", "header_version", "page_size", "kernel_size", "kernel_addr",
              "ramdisk_size", "ramdisk_addr", "second_size", "second_addr", "tags_addr",
              "recovery_dtbo_size", "dtb_size", "dtb_addr", "name", "id"):
        if k in h:
            v = h[k]
            print(f"  {k:18} {v if not isinstance(v, int) else f'{v} (0x{v:x})'}" if k.endswith("_addr") or k.endswith("_size") or k in ("page_size", "header_version", "dtb_size", "recovery_dtbo_size") else f"  {k:18} {v}")
    print(f"  {'cmdline':18} {h['cmdline']}")
    if h["extra_cmdline"]:
        print(f"  {'extra_cmdline':18} {h['extra_cmdline']}")
    print(f"  end of parsable area  offset={sl['_end'][0]} trailing={sl['_end'][1]}B")

    # kernel
    koff, klen = sl["kernel"]
    kraw = buf[koff:koff + klen]
    (out / "kernel.raw").write_bytes(kraw)
    kdec, kcomp = decompress_kernel(kraw)
    (out / "kernel.bin").write_bytes(kdec)
    print(f"\n  kernel: {klen} B raw, compression={kcomp}, decompressed={len(kdec)} B")

    # ramdisk
    if h["ramdisk_size"]:
        roff, rlen = sl["ramdisk"]
        (out / "ramdisk.cpio.gz").write_bytes(buf[roff:roff + rlen])
        print(f"  ramdisk: {rlen} B -> {out/'ramdisk.cpio.gz'}")

    # dtb(s): explicit slice + every FDT magic in the tail
    blobs: list[tuple[str, bytes]] = []
    if "dtb" in sl:
        doff, dlen = sl["dtb"]
        blobs.append(("dtb (header v2)", buf[doff:doff + dlen]))
    pos = 0
    n = 0
    while True:
        pos = buf.find(struct.pack(">I", FDT_MAGIC), pos)
        if pos < 0:
            break
        n += 1
        blobs.append((f"fdt@{pos:#x}", buf[pos:pos + 4 * 1024 * 1024]))
        pos += 4
    print(f"  FDT magic occurrences: {n}")

    report = {"header": h, "kernel_compression": kcomp, "fdt_count": n}
    for label, blob in blobs[:8]:
        try:
            t = FDT(blob)
        except Exception as e:  # noqa: BLE001
            print(f"  {label}: not a valid FDT ({e})")
            continue
        name = re.sub(r"[^A-Za-z0-9_.@-]", "_", label)
        (out / f"{name}.dtb").write_bytes(t.struct)  # 结构块，便于回看
        (out / f"{name}.dts").write_text(t.dump(), encoding="utf-8")
        print(f"\n  FDT {label}: version={t.version} size={t.total} nodes={sum(1 for d, nm, p in t.walk() if nm)}")
        if args.dump_dtb:
            print(t.dump())
        else:
            # 只打印关键分支
            keep = re.compile(r"(panel|dsi|touch|synaptics|i2c@c175000|mdss|memory|chosen|reserved-memory|splash|qcom,board-id|oppo|display)", re.I)
            depth0 = 0
            for depth, nm, prop in t.walk():
                if nm is not None:
                    depth0 = depth
                    if keep.search(nm):
                        print("    " + "  " * depth + nm)
                elif prop and depth0 <= 3 and keep.search(prop[0]):
                    print("    " + "  " * depth + f"{prop[0]} = {FDT._fmt(prop[1], 48)}")
            print(f"    (full dump -> {out/f'{name}.dts'})")

    scan_strings(kdec, "kernel (decompressed)")
    for label, blob in blobs[:1]:
        scan_strings(blob[:1 << 20], label)

    if args.json:
        (out / "header.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\n  header.json -> {out/'header.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
