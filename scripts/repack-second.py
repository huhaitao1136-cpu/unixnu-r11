#!/usr/bin/env python3
"""把 boot.img 里的 payload 从 ramdisk 改成 second 段。

R11 的 ABL 对 ramdisk 有 32 MiB 上限（实测：32 MiB 能跑，36 MiB 卡在开机 logo），
但 second 段没有这个限制（实测 57 MiB 正常）。而 UniXNU 的 payload 有 51–56 MiB。

做法：
  1. 读 unixnu bootimg 产出的 boot.img，取出 kernel 段（= gzip(BootShim+FD) + DTB）
  2. 用 mkbootimg 重新打包：--kernel 那段 --ramdisk 空 --second payload.img
  3. 覆盖 boot.img
"""
import argparse
import struct
import subprocess
import sys
from pathlib import Path

PAGE = 4096


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

    scratch = args.bootimg.parent
    kpath = scratch / '.repack-kernel.bin'
    rdpath = scratch / '.repack-ramdisk.bin'
    kpath.write_bytes(kernel)
    rdpath.write_bytes(b'')

    cmd = [sys.executable, str(args.mkbootimg),
           '--kernel', str(kpath), '--ramdisk', str(rdpath),
           '--second', str(args.payload), '-o', str(args.bootimg),
           '--base', str(args.base), '--kernel_offset', str(args.kernel_offset),
           '--tags_offset', str(args.tags_offset), '--pagesize', str(PAGE),
           '--header_version', str(args.header_version)]
    print('+ ' + ' '.join(cmd))
    subprocess.run(cmd, check=True)

    out = args.bootimg.read_bytes()
    ks, _, rs, _, ss, sa, _, page, _, _ = struct.unpack_from('<10I', out, 8)
    print(f'repack: kernel {ks} ramdisk {rs} second {ss} @0x{sa:x}, 总 {len(out)} B')
    kpath.unlink(missing_ok=True)
    rdpath.unlink(missing_ok=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
