#!/usr/bin/env bash
# 在 macOS 上生成一个"最小 Data 种子"（DBRDISK1 打包的 APFS 容器）。
#
# 种子里只有最基本的目录与偏好设置：
#   /private/var/mobile/Library/Preferences/com.apple.purplebuddy.plist  (跳过 Setup)
# 内核能把它当 BSD root 挂载，从而越过 IOFindBSDRoot。
#
# 同时打印并写出 APFS 卷的 UUID（board.toml 的 root_match_volume_uuid 要用它）。
#
# 用法: bash make-data-seed.sh <输出 seed.dbr> [逻辑大小MB]
#   例: bash make-data-seed.sh out/seed.dbr 4096
# 产物:
#   <输出>        打包好的 DBRDISK1
#   <输出>.uuid   APFS 卷 UUID（大写十六进制）
set -euo pipefail

OUT="${1:?用法: make-data-seed.sh <输出.dbr> [逻辑MB]}"
SIZE_MB="${2:-4096}"
WORK="$(mktemp -d /tmp/r11-seed-XXXXXX)"
RAW="$WORK/seed.raw"
MNT="$WORK/mnt"

echo "== 建 ${SIZE_MB} MB APFS 镜像"
mkdir -p "$MNT"
hdiutil create -size "${SIZE_MB}m" -fs APFS -layout NONE -volname "Data" "$WORK/seed.dmg" >/dev/null
hdiutil attach "$WORK/seed.dmg" -mountpoint "$MNT" -nobrowse >/dev/null

echo "== 读取 APFS 卷 UUID"
VOL_UUID="$(diskutil info "$MNT" 2>/dev/null | grep -i 'Volume UUID' | head -1 | sed 's/.*: *//')"
if [ -z "${VOL_UUID:-}" ]; then
  echo "!! 读不到 Volume UUID，退出"
  hdiutil detach "$MNT" >/dev/null 2>&1 || true
  exit 1
fi
VOL_UUID="$(echo "$VOL_UUID" | tr 'a-f' 'A-F')"
echo "   UUID = $VOL_UUID"
printf '%s\n' "$VOL_UUID" > "${OUT}.uuid"

echo "== 写入最小目录与偏好"
mkdir -p "$MNT/private/var/mobile/Library/Preferences"
mkdir -p "$MNT/private/var/mobile/Containers/Data/Application"
mkdir -p "$MNT/private/var/mobile/Containers/Shared/SystemGroup"
mkdir -p "$MNT/private/var/mobile/Media"
mkdir -p "$MNT/private/var/db"
chmod 755 "$MNT/private" "$MNT/private/var" "$MNT/private/var/mobile" 2>/dev/null || true

python3 - "$MNT/private/var/mobile/Library/Preferences/com.apple.purplebuddy.plist" <<'PY'
import plistlib, sys
from pathlib import Path
p = Path(sys.argv[1])
p.write_bytes(plistlib.dumps({
    'SetupDone': True,
    'SetupFinishedAllSteps': True,
    'SetupVersion': 11,
    'daemonizeNeedsToRun': False,
}, fmt=plistlib.FMT_BINARY))
print('写入', p)
PY

chmod 600 "$MNT/private/var/mobile/Library/Preferences/com.apple.purplebuddy.plist" || true

echo "== 卸载并展开成 raw"
sync
hdiutil detach "$MNT" >/dev/null
hdiutil convert "$WORK/seed.dmg" -format UDRO -o "$WORK/seed.img" >/dev/null
if [ -f "$WORK/seed.img.dmg" ]; then
  hdiutil convert "$WORK/seed.img.dmg" -format UDRW -o "$WORK/seed-rw" >/dev/null
  RAW="$WORK/seed-rw.dmg"
fi

echo "== 打包为 DBRDISK1"
python3 - "$RAW" "$OUT" <<'PY'
import sys, importlib.util
from pathlib import Path
raw, out = Path(sys.argv[1]), Path(sys.argv[2])
import os
cands = []
# 先看几个已知位置
for base in ('.', '..', 'UniXNU', '../UniXNU'):
    b = Path(base)
    if not b.is_dir():
        continue
    for rel in ('drivers/DarwinBoot/Test/packed_ramdisk.py',
                'Test/packed_ramdisk.py'):
        f = b / rel
        if f.is_file():
            cands.append(f)
# 再做一次有限深度的搜索
if not cands:
    for base in ('..', '.', '../UniXNU', 'UniXNU'):
        b = Path(base)
        if not b.is_dir():
            continue
        for root, dirs, files in os.walk(b):
            # 跳过体积大的目录
            dirs[:] = [d for d in dirs if d not in ('.git', 'out', 'build', 'refer', '.cache')]
            if 'packed_ramdisk.py' in files:
                cands.append(Path(root) / 'packed_ramdisk.py')
                break
        if cands:
            break
if not cands:
    sys.exit('找不到 packed_ramdisk.py')
spec = importlib.util.spec_from_file_location('packed_ramdisk', cands[0])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
mod.pack(raw, out)
print(f'打包完成: {out} ({out.stat().st_size} B)')
PY

rm -rf "$WORK"
echo "== 完成: $OUT"
ls -l "$OUT" "${OUT}.uuid"
