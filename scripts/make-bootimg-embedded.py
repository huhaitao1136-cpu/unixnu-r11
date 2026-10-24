#!/usr/bin/env python3
"""把 payload 嵌进 boot.img 的 kernel 段（ABL 会把它整体解压到内存）。

背景（实测）：
  * R11 的 ABL 对 ramdisk 有 32 MiB 上限（32 MiB 可启动，36 MiB 卡开机 logo）
  * second 段会被接受（镜像不报错），但 ABL **不会**把内容加载进内存（全内存扫描找不到）
  * 而 kernel 段（gzip 流）会被 ABL 解压到 RAM —— 所以把 payload 接在
    BootShim + FD 后面一起 gzip，解压后 payload 就以原始字节留在内存里，
    固件的 R11BootRamdisk 扫描内存即可找到那个 FAT 卷。

做法：
  1. 读 unixnu bootimg 产出的 boot.img（--ramdisk none），取出 kernel 段
  2. 该段 = gzip(BootShim + FD) + DTB；用 zlib 分离出 gzip 流和 DTB
  3. 解压 gzip 得到 BootShim + FD，接上 payload.img（按 4 KiB 对齐）
  4. 重新 gzip，再拼回 DTB，用 mkbootimg 打包（无 ramdisk、无 second）
"""
import argparse
import gzip
import struct
import subprocess
import sys
import zlib
from pathlib import Path

PAGE = 4096


def split_kernel(kernel: bytes):
    """把 Image.gz-dtb 分成 (解压后的 image, 追加的 dtb)。"""
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    image = d.decompress(kernel)
    if not d.eof:
        raise SystemExit('kernel gzip stream is truncated')
    return image, d.unused_data


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--bootimg', type=Path, required=True)
    ap.add_argument('--payload', type=Path, required=True)
    ap.add_argument('--mkbootimg', type=Path, required=True)
    ap.add_argument('--base', default='0')
    ap.add_argument('--kernel-offset', default='0x8000')
    ap.add_argument('--tags-offset', default='0x100')
    ap.add_argument('--header-version', default='0')
    args = ap.parse_args()

    data = args.bootimg.read_bytes()
    if data[:8] != b'ANDROID!':
        raise SystemExit(f'{args.bootimg}: not an Android boot image')
    kernel_size = struct.unpack_from('<I', data, 8)[0]
    kernel = data[PAGE:PAGE + kernel_size]

    image, dtb = split_kernel(kernel)
    payload = args.payload.read_bytes()
    payload += b'\0' * (-len(payload) % PAGE)
    combined = image + payload

    scratch = args.bootimg.parent
    kpath = scratch / '.embed-kernel.bin'
    rdpath = scratch / '.embed-ramdisk.bin'
    kpath.write_bytes(gzip.compress(combined, compresslevel=6, mtime=0) + dtb)
    rdpath.write_bytes(b'')

    cmd = [sys.executable, str(args.mkbootimg),
           '--kernel', str(kpath), '--ramdisk', str(rdpath),
           '-o', str(args.bootimg),
           '--base', str(args.base), '--kernel_offset', str(args.kernel_offset),
           '--tags_offset', str(args.tags_offset), '--pagesize', str(PAGE),
           '--header_version', str(args.header_version)]
    print('+ ' + ' '.join(cmd))
    subprocess.run(cmd, check=True)

    out = args.bootimg.read_bytes()
    ks, _, rs, _, ss, page, _, _, _, _ = struct.unpack_from('<10I', out, 8)[:10]
    print(f'embed: image {len(image)} + payload {len(payload)} = {len(combined)} B 解压后')
    print(f'       kernel 段 {ks} B（gzip），总 {len(out)} B')
    kpath.unlink(missing_ok=True)
    rdpath.unlink(missing_ok=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
