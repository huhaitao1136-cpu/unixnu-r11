#!/usr/bin/env bash
# 在 macOS 上生成一个"最小 Data 种子"（DBRDISK1 打包的 APFS 容器）。
#
# 这个种子里只有最基本的目录与偏好设置：
#   /private/var/mobile/Library/Preferences/com.apple.purplebuddy.plist  (跳过 Setup)
# 内核能把它当 BSD root 挂载，从而越过 IOFindBSDRoot。
#
# 用法: bash make-data-seed.sh <输出 seed.dbr> [逻辑大小MB]
#   例: bash make-data-seed.sh out/seed.dbr 4096
set -euo pipefail

OUT="${1:?用法: make-data-seed.sh <输出.dbr> [逻辑MB]}"
SIZE_MB="${2:-4096}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d /tmp/r11-seed-XXXXXX)"
RAW="$WORK/seed.raw"
MNT="$WORK/mnt"

echo "== 建 ${SIZE_MB} MB 稀疏镜像"
mkdir -p "$MNT"
# hdiutil 创建稀疏磁盘镜像（APFS）
hdiutil create -size "${SIZE_MB}m" -fs APFS -layout NONE -volname "Data" "$WORK/seed.dmg" >/dev/null
hdiutil attach "$WORK/seed.dmg" -mountpoint "$MNT" -nobrowse >/dev/null

echo "== 写入最小目录与偏好"
mkdir -p "$MNT/private/var/mobile/Library/Preferences"
mkdir -p "$MNT/private/var/mobile/Containers/Data/Application"
mkdir -p "$MNT/private/var/mobile/Containers/Shared/SystemGroup"
mkdir -p "$MNT/private/var/mobile/Media"
mkdir -p "$MNT/private/var/db"
chmod 755 "$MNT/private" "$MNT/private/var" "$MNT/private/var/mobile" 2>/dev/null || true

# 让 Setup 直接视为完成（否则会停在 Hello 设置页面）
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
# UDRO 产物是 .img.dmg，展开出裸镜像
if [[ -f "$WORK/seed.img.dmg" ]]; then
  hdiutil convert "$WORK/seed.img.dmg" -format UDRW -o "$WORK/seed-rw" >/dev/null
  RAW="$WORK/seed-rw.dmg"
fi

echo "== 打包为 DBRDISK1"
python3 - "$RAW" "$OUT" <<'PY'
import sys, importlib.util
from pathlib import Path
raw, out = Path(sys.argv[1]), Path(sys.argv[2])
# 找到 UnioXNU 里的 packed_ramdisk.py
cands = list(Path('.').glob('UniXNU/drivers/DarwinBoot/Test/packed_ramdisk.py')) + \
        list(Path('..').glob('UniXNU/drivers/DarwinBoot/Test/packed_ramdisk.py')) + \
        list(Path('.').glob('drivers/DarwinBoot/Test/packed_ramdisk.py'))
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
ls -l "$OUT"
