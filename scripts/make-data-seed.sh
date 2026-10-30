#!/usr/bin/env bash
# 在 macOS 上生成"最小 Data 种子"。
#
# 产出：
#   <out>.raw   稠密的原始 APFS 镜像 —— XNU 内置 ramdisk 路径（--data-ramdisk）用它
#   <out>.dbr   DBRDISK1 打包版 —— UniXNU 的 packed 路径用它（需要 NativeRamBlock 驱动）
#   <out>.uuid  APFS 卷 UUID（大写）
#
# 用法: bash make-data-seed.sh <输出前缀> [逻辑大小MB，默认 48]
set -euo pipefail

OUT="${1:?用法: make-data-seed.sh <输出前缀> [逻辑MB]}"
SIZE_MB="${2:-32}"
WORK="$(mktemp -d /tmp/r11-seed-XXXXXX)"
MNT="$WORK/mnt"

echo "== 建 ${SIZE_MB} MB APFS 镜像"
mkdir -p "$MNT"
hdiutil create -size "${SIZE_MB}m" -fs APFS -layout NONE -volname "OwnerData" "$WORK/seed.dmg" >/dev/null
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

# /dev 必须存在，否则 bsd_init 里的 `devfs_kernel_mount("/dev")` 返回 ENOENT
# （真机日志：`devfs_kernel_mount: kernel_mount failed: 2`），随后 PID 1 打开
# /dev/console 会失败 → 用户态任何输出都看不见。空的挂载点就够了。
mkdir -p "$MNT/dev"
chmod 755 "$MNT/dev"

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

# ---- 最小 init：验证 EL0 用户态执行 ----
# kern_exec.c 的候选顺序（第 7104-7109 行）：
#   /usr/appleinternal/sbin/launchd.debug
#   /usr/appleinternal/sbin/launchd.development
#   /sbin/launchd
# 三处都放同一份最小程序，确保任意分支都能 exec 成功。
# 用哪个程序当 PID 1：默认我们手搓的 init.macho，
# 用 R11_INIT_FILE=launchd.apple 可换成苹果真正的 /sbin/launchd。
INIT_FILE="${R11_INIT_FILE:-init.macho}"
INIT_BIN="$(cd "$(dirname "$0")/.." && pwd)/seed/$INIT_FILE"
if [ -f "$INIT_BIN" ]; then
  echo "== 安装 PID 1（$INIT_FILE, $(stat -f%z "$INIT_BIN") B）"
  mkdir -p "$MNT/sbin" "$MNT/usr/appleinternal/sbin"
  # kern_exec.c 的候选顺序：launchd.debug → launchd.development → /sbin/launchd
  # 目录必须存在且可遍历，否则内核 namei 会返回 ENOENT
  chmod 755 "$MNT/usr" "$MNT/usr/appleinternal" "$MNT/usr/appleinternal/sbin" "$MNT/sbin" 2>/dev/null || true
  for target in /usr/appleinternal/sbin/launchd.debug \
                /usr/appleinternal/sbin/launchd.development \
                /sbin/launchd; do
    cp "$INIT_BIN" "$MNT$target"
    chmod 755 "$MNT$target"
    echo "   $target  $(ls -l "$MNT$target" | awk '{print $1, $5}')"
  done
else
  echo "!! 找不到 $INIT_BIN，seed 里不会有 init"
fi

echo "== 卸载并转成稠密 raw（UDRW）"
sync
hdiutil detach "$MNT" >/dev/null
rm -f "$WORK/seed-rw.dmg"
hdiutil convert "$WORK/seed.dmg" -format UDRW -o "$WORK/seed-rw" >/dev/null
RAW="$WORK/seed-rw.dmg"
[ -f "$RAW" ] || RAW="$WORK/seed-rw"
cp "$RAW" "${OUT}.raw"
echo "   稠密镜像: $(ls -lh "${OUT}.raw" | awk '{print $5}')"

echo "== 打包为 DBRDISK1（可选）"
python3 - "$RAW" "${OUT}.dbr" <<'PY'
import sys, importlib.util, os
from pathlib import Path
raw, out = Path(sys.argv[1]), Path(sys.argv[2])
cands = []
for base in ('.', '..', 'UniXNU', '../UniXNU', 'UniXNU/drivers/DarwinBoot'):
    b = Path(base)
    if not b.is_dir():
        continue
    for rel in ('drivers/DarwinBoot/Test/packed_ramdisk.py', 'Test/packed_ramdisk.py'):
        f = b / rel
        if f.is_file():
            cands.append(f)
if not cands:
    for base in ('..', '.', '../UniXNU', 'UniXNU'):
        b = Path(base)
        if not b.is_dir():
            continue
        for root, dirs, files in os.walk(b):
            dirs[:] = [d for d in dirs if d not in ('.git', 'out', 'build', 'refer', '.cache')]
            if 'packed_ramdisk.py' in files:
                cands.append(Path(root) / 'packed_ramdisk.py')
                break
        if cands:
            break
if not cands:
    print('跳过 DBRDISK1（找不到 packed_ramdisk.py），raw 已经生成')
    raise SystemExit(0)
spec = importlib.util.spec_from_file_location('packed_ramdisk', cands[0])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
mod.pack(raw, out)
print(f'打包完成: {out} ({out.stat().st_size} B)')
PY

rm -rf "$WORK"
echo "== 完成"
ls -l "${OUT}.uuid" "${OUT}.raw" 2>/dev/null || true
ls -l "${OUT}.dbr" 2>/dev/null || true
