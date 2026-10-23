#!/usr/bin/env python3
"""make-payload-fat.py — 生成 FAT12 镜像（支持目录树）。

用法:
    python3 make-payload-fat.py <输出> [--size-kb 64] [--add 镜像内路径=源文件 ...]

例子:
    python3 make-payload-fat.py payload.fat --add EFI/BOOT/BOOTAA64.EFI=app.efi

零依赖，纯标准库。每个目录占一个扇区（≤14 项），对本用途足够。
"""
import struct
import sys

SECTOR = 512
ROOT_ENTRIES = 16
MEDIA = 0xF8
FAT_COUNT = 2
FAT_SECTORS = 1
ATTR_DIR = 0x10
ATTR_ARCHIVE = 0x20


def short_name(name):
    if "." in name:
        base, ext = name.rsplit(".", 1)
    else:
        base, ext = name, ""
    return (base.upper()[:8].ljust(8) + ext.upper()[:3].ljust(3)).encode("ascii")


def entry(name, attr, cluster, size):
    e = bytearray(32)
    if attr == ATTR_DIR:
        e[0:11] = name.upper()[:8].ljust(8).encode("ascii") + b"   "
    else:
        e[0:11] = short_name(name)
    e[11] = attr
    struct.pack_into("<H", e, 26, cluster)
    struct.pack_into("<I", e, 28, size)
    return bytes(e)


def fat12_set(fat, cluster, value):
    off = cluster + cluster // 2
    if cluster & 1:
        fat[off] = (fat[off] & 0x0F) | ((value << 4) & 0xF0)
        fat[off + 1] = (value >> 4) & 0xFF
    else:
        fat[off] = value & 0xFF
        fat[off + 1] = (fat[off + 1] & 0xF0) | ((value >> 8) & 0x0F)


def build(size_kb, files, out_path):
    total = size_kb * 1024 // SECTOR
    data_start = 1 + FAT_COUNT * FAT_SECTORS + (ROOT_ENTRIES * 32) // SECTOR
    clusters_n = total - data_start
    if clusters_n <= 0 or clusters_n >= 4085:
        raise SystemExit("卷大小不适合 FAT12")

    root = {"dirs": {}, "files": {}}
    for path, content in files:
        parts = [p for p in path.strip("/").split("/") if p]
        if not parts:
            continue
        node = root
        for d in parts[:-1]:
            node = node["dirs"].setdefault(d, {"dirs": {}, "files": {}})
        node["files"][parts[-1]] = content

    img = bytearray(total * SECTOR)
    fat = bytearray(FAT_SECTORS * SECTOR)
    fat[0:3] = bytes([MEDIA, 0xFF, 0xFF])
    nxt = [2]

    def alloc(n=1):
        first = nxt[0]
        nxt[0] += n
        return first

    def chain(start, n):
        for i in range(n):
            fat12_set(fat, start + i, 0xFFF if i == n - 1 else start + i + 1)

    def cluster_off(c):
        return SECTOR * (data_start + (c - 2))

    def assign_dirs(node):
        node["cluster"] = alloc(1)
        chain(node["cluster"], 1)
        for sub in node["dirs"].values():
            assign_dirs(sub)

    assign_dirs(root)

    def assign_files(node):
        for name, content in list(node["files"].items()):
            need = max(1, (len(content) + SECTOR - 1) // SECTOR)
            start = alloc(need)
            chain(start, need)
            node["files"][name] = (start, len(content), content)
        for sub in node["dirs"].values():
            assign_files(sub)

    assign_files(root)

    def write_dir(node, is_root):
        ents = []
        if not is_root:
            ents.append(entry(".", ATTR_DIR, node["cluster"], 0))
            ents.append(entry("..", ATTR_DIR, 0, 0))
        for name, sub in node["dirs"].items():
            ents.append(entry(name, ATTR_DIR, sub["cluster"], 0))
        for name, (start, size, _) in node["files"].items():
            ents.append(entry(name, ATTR_ARCHIVE, start, size))
        if len(ents) > 14:
            raise SystemExit(f"目录项太多（{len(ents)}），简化结构")
        if is_root:
            base = SECTOR * (1 + FAT_COUNT * FAT_SECTORS)
        else:
            base = cluster_off(node["cluster"])
        for i, e in enumerate(ents):
            img[base + i * 32: base + (i + 1) * 32] = e
        for sub in node["dirs"].values():
            write_dir(sub, False)

    write_dir(root, True)

    def write_files(node):
        for name, (start, size, content) in node["files"].items():
            off = cluster_off(start)
            img[off:off + size] = content
        for sub in node["dirs"].values():
            write_files(sub)

    write_files(root)

    bs = bytearray(SECTOR)
    bs[0:3] = b"\xEB\x3C\x90"
    bs[3:11] = b"R11PAY  "
    struct.pack_into("<H", bs, 11, SECTOR)
    bs[13] = 1
    struct.pack_into("<H", bs, 14, 1)
    bs[16] = FAT_COUNT
    struct.pack_into("<H", bs, 17, ROOT_ENTRIES)
    struct.pack_into("<H", bs, 19, total if total < 65536 else 0)
    bs[21] = MEDIA
    struct.pack_into("<H", bs, 22, FAT_SECTORS)
    struct.pack_into("<H", bs, 24, 32)
    struct.pack_into("<H", bs, 26, 2)
    struct.pack_into("<I", bs, 39, 0x52313131)
    bs[43:54] = b"R11PAYLOAD "
    bs[54:62] = b"FAT12   "
    bs[510:512] = b"\x55\xAA"
    img[0:SECTOR] = bs

    img[SECTOR:SECTOR * (1 + FAT_SECTORS)] = fat
    img[SECTOR * (1 + FAT_SECTORS):SECTOR * (1 + 2 * FAT_SECTORS)] = fat

    with open(out_path, "wb") as f:
        f.write(img)
    return len(img), len(files)


def main():
    out = sys.argv[1]
    size_kb = 64
    files = []
    i = 2
    while i < len(sys.argv):
        if sys.argv[i] == "--size-kb":
            size_kb = int(sys.argv[i + 1]); i += 2
        elif sys.argv[i] == "--add":
            path, src = sys.argv[i + 1].split("=", 1)
            files.append((path, open(src, "rb").read())); i += 2
        else:
            raise SystemExit(f"未知参数 {sys.argv[i]}")

    if not files:
        files = [("README.TXT",
                  b"R11 UniXNU payload volume.\n"
                  b"Built into the firmware volume as a RAW section;\n"
                  b"published as BlockIo by R11RamDisk.\n")]

    size, n = build(size_kb, files, out)
    print(f"{out}: {size} B, {n} 个文件")


if __name__ == "__main__":
    main()
