#!/usr/bin/env python3
"""check-r11.py — 板级预检：`[firmware] preflight` 用。

校验：
  1. 平台包里引用的每个 `Binaries/r11/...inf` 都存在，且 INF 里点名的 PE32 也在；
  2. `r11.fdf` 引用的 RawFiles 都存在；
  3. 固件卷大小不超过 `TARGET_FD_SIZE`（构建产物存在时）。

用法：
  python check-r11.py --platform ../uefi-r11/Platforms/OPPO/r11Pkg \
      --binaries ../uefi-r11/Binaries/r11 [--fd Build/r11Pkg/RELEASE_CLANGPDB/FV/SILICIUM_UEFI.fd]
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


def _guess(rel: str) -> Path | None:
    """预检脚本由构建驱动无参调用，所以自己找位置：
    cwd 是构建树（可能已把 uefi-r11 合并进来），也可能在仓库里直接跑。"""
    here = Path(__file__).resolve().parent
    for base in (Path.cwd(), here, here.parent, here.parent.parent):
        for cand in (base / rel, base / "uefi-r11" / rel, base.parent / "uefi-r11" / rel):
            if cand.exists():
                return cand
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform", help="平台包目录（默认自动探测）")
    ap.add_argument("--binaries", help="Binaries/r11 目录（默认自动探测）")
    ap.add_argument("--fd")
    args = ap.parse_args()

    plat = Path(args.platform) if args.platform else _guess("Platforms/OPPO/r11Pkg")
    bins = Path(args.binaries) if args.binaries else _guess("Binaries/r11")
    if plat is None or bins is None:
        print(f"check-r11: 跳过（platform={plat}, binaries={bins}）；用 --platform/--binaries 指定")
        return 0
    problems: list[str] = []

    refs: set[str] = set()
    raw_refs: set[str] = set()
    for f in list(plat.rglob("*.inc")) + list(plat.rglob("*.fdf")):
        t = f.read_text(encoding="utf-8", errors="replace")
        refs.update(re.findall(r"Binaries/r11/(\S+\.inf)", t))
        raw_refs.update(re.findall(r"Binaries/r11/RawFiles/(\S+)", t))

    print(f"[1] 厂商 INF: {len(refs)} 个")
    for rel in sorted(refs):
        inf = bins / rel
        if not inf.exists():
            problems.append(f"缺少 {rel}")
            continue
        t = inf.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"PE32\|(\S+\.efi)", t)
        if not m:
            problems.append(f"{rel} 里没有 PE32 行")
            continue
        pe = inf.parent / m.group(1)
        if not pe.exists():
            problems.append(f"{rel} 指向的 {m.group(1)} 不存在")

    print(f"[2] RawFiles: {len(raw_refs)} 个")
    for rel in sorted(raw_refs):
        if not (bins / "RawFiles" / rel).exists():
            problems.append(f"缺少 RawFiles/{rel}")

    if args.fd:
        fd = Path(args.fd)
        if fd.exists():
            size = fd.stat().st_size
            print(f"[3] 固件卷: {size} B (0x{size:x})")
            cfgs = list(plat.glob("../../Resources/Configs/*.conf"))
            for c in cfgs:
                m = re.search(r'TARGET_FD_SIZE="0x([0-9A-Fa-f]+)"', c.read_text(errors="replace"))
                if m and size > int(m.group(1), 16):
                    problems.append(f"固件卷 {size} B 超过 TARGET_FD_SIZE {m.group(1)}")

    if problems:
        print("\n预检失败：")
        for p in problems:
            print("  - " + p)
        return 1
    print("\n预检通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
