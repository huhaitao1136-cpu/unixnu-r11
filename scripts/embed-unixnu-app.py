#!/usr/bin/env python3
"""把 UniXNU.efi 写进 payload 卷（EFI/BOOT/BOOTAA64.EFI）。

方案 A：让固件按"文件路径"加载启动管理器（EDK2 标准用法），
而不是把 118 KB 的 PE32 缓冲区塞给 LoadImage。

payload.img 是 FAT32，用 mtools 操作：
  mmd  -i payload.img ::/EFI/BOOT
  mcopy -i payload.img UniXNU.efi ::/EFI/BOOT/BOOTAA64.EFI
"""
import shutil
import subprocess
import sys
from pathlib import Path


def run(cmd):
    print("+ " + " ".join(str(c) for c in cmd))
    subprocess.run([str(c) for c in cmd], check=True)


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    payload = Path(sys.argv[1])
    app = Path(sys.argv[2])

    for tool in ("mmd", "mcopy"):
        if shutil.which(tool) is None:
            print(f"!! {tool} 缺失（apt-get install mtools）", file=sys.stderr)
            return 1
    if not payload.is_file():
        print(f"!! payload 不存在: {payload}", file=sys.stderr)
        return 1
    if not app.is_file():
        print(f"!! UniXNU.efi 不存在: {app}", file=sys.stderr)
        return 1

    # 目录可能已存在；mmd 会提示覆盖并等键盘输入 → 必须禁用 stdin。
    # 实际上 payload 里已经有 EFI/BOOT，直接跳过 mmd 更稳。
    if subprocess.run(["mdir", "-i", str(payload), "::/EFI/BOOT"],
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                      stdin=subprocess.DEVNULL).returncode != 0:
        subprocess.run(["mmd", "-i", str(payload), "::/EFI/BOOT"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       stdin=subprocess.DEVNULL)
    run(["mcopy", "-o", "-i", str(payload), str(app), "::/EFI/BOOT/BOOTAA64.EFI"])

    listing = subprocess.run(["mdir", "-i", str(payload), "::/EFI/BOOT"],
                             capture_output=True, text=True)
    print(listing.stdout[-400:])
    print(f"已把 {app.name} ({app.stat().st_size} B) 写入 payload 卷")
    return 0


if __name__ == "__main__":
    sys.exit(main())
