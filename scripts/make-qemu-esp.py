#!/usr/bin/env python3
"""为 QEMU 拼一个能启动 XNU 的 ESP 目录。

产物布局（AAVMF 固件 + qemu-run.py 直接可用）：
  EFI/BOOT/BOOTAA64.EFI            UniXNU 启动管理器
  EFI/UniXNU/unixnu.toml           配置：driver = DarwinBoot.efi，entry = xnu
  EFI/UniXNU/Drivers/DarwinBoot.efi
  EFI/UniXNU/Payloads/kernel.macho
  EFI/UniXNU/Payloads/kernel.macho.adt

用法：
  python3 make-qemu-esp.py --out out/esp-qemu \
      --unixnu-app <UniXNU.efi> --darwinboot <DarwinBoot.efi> \
      --kernel <kernel.macho> --adt <kernel.macho.adt>
"""
import argparse
import shutil
import sys
from pathlib import Path

TOML = """# QEMU virt + AAVMF：跑我们的 XNU 内核
version = 1

[boot]
timeout = 3
default = "xnu"
rotate = 0

[ui]
title = "R11 XNU (QEMU)"

[[driver]]
path = "Drivers/DarwinBoot.efi"
enabled = true

[[entry]]
id = "xnu"
title = "XNU kernel"
type = "xnu"
path = "Payloads/kernel.macho"
args = "-v serial=3 debug=0x8"
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--unixnu-app', type=Path, required=True)
    ap.add_argument('--darwinboot', type=Path, required=True)
    ap.add_argument('--kernel', type=Path, required=True)
    ap.add_argument('--adt', type=Path, required=True)
    args = ap.parse_args()

    for p in (args.unixnu_app, args.darwinboot, args.kernel, args.adt):
        if not p.is_file():
            print(f'!! 缺少 {p}', file=sys.stderr)
            return 1

    out = args.out
    if out.exists():
        shutil.rmtree(out)
    (out / 'EFI/BOOT').mkdir(parents=True)
    (out / 'EFI/UniXNU/Drivers').mkdir(parents=True)
    (out / 'EFI/UniXNU/Payloads').mkdir(parents=True)

    shutil.copyfile(args.unixnu_app, out / 'EFI/BOOT/BOOTAA64.EFI')
    shutil.copyfile(args.darwinboot, out / 'EFI/UniXNU/Drivers/DarwinBoot.efi')
    shutil.copyfile(args.kernel, out / 'EFI/UniXNU/Payloads/kernel.macho')
    shutil.copyfile(args.adt, out / 'EFI/UniXNU/Payloads/kernel.macho.adt')
    (out / 'EFI/UniXNU/unixnu.toml').write_text(TOML, encoding='utf-8')

    for f in sorted(out.rglob('*')):
        if f.is_file():
            print(f'  {f.relative_to(out)}  {f.stat().st_size} B')
    print(f'ESP 就绪: {out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
