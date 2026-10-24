#!/usr/bin/env bash
# 在 QEMU 里跑一次 R11 的内核，并打印串口日志 + 内核 debug 缓冲区 + 截屏。
#
# 用法:
#   bash qemu-run-r11.sh [内核文件] [输出前缀]
# 例:
#   bash qemu-run-r11.sh /tmp/kernel-pan4.macho run1
#
# 需要: qemu-system-aarch64, AAVMF, mtools, make-qemu-esp.py, read-debug-buf.py
set -u

KERNEL="${1:-/root/src/UniXNU/out/r11/kernel/kernel.macho}"
PREFIX="${2:-run}"
SELF="$(cd "$(dirname "$0")" && pwd)"
MU=/root/src/Mu-Silicium/Build/r11Pkg/DEBUG_CLANGDWARF/AARCH64
APP="$MU/UniXNUPkg/Application/UniXNU/UniXNU/OUTPUT/UniXNU.efi"
DB="$MU/DarwinBootPkg/Driver/DarwinBoot/OUTPUT/DarwinBoot.efi"
WORK=/tmp/r11qemu
ADT=/tmp/qemu.adt
LOG=/tmp/${PREFIX}.log
QMP=/tmp/${PREFIX}-qmp.sock

fail() { echo "!! $*"; exit 1; }

for f in "$APP" "$DB" "$KERNEL" "$ADT"; do
  [[ -f "$f" ]] || fail "缺少 $f（ADT 用 make-r11-adt.py --dram-base 0x40000000 生成）"
done

echo "== 拼 ESP"
rm -rf "$WORK/esp"
python3 "$SELF/make-qemu-esp.py" --out "$WORK/esp" \
  --unixnu-app "$APP" --darwinboot "$DB" \
  --kernel "$KERNEL" --adt "$ADT" >/dev/null || fail "make-qemu-esp 失败"

echo "== 做 FAT32 镜像"
rm -f "$WORK/esp.img"
dd if=/dev/zero of="$WORK/esp.img" bs=1M count=110 status=none || fail "dd 失败"
mkfs.vfat -F 32 "$WORK/esp.img" >/dev/null || fail "mkfs.vfat 失败"
mmd -i "$WORK/esp.img" ::/EFI ::/EFI/BOOT ::/EFI/UniXNU ::/EFI/UniXNU/Drivers ::/EFI/UniXNU/Payloads
mcopy -i "$WORK/esp.img" "$WORK/esp/EFI/BOOT/BOOTAA64.EFI" ::/EFI/BOOT/
mcopy -i "$WORK/esp.img" "$WORK/esp/EFI/UniXNU/unixnu.toml" ::/EFI/UniXNU/
mcopy -i "$WORK/esp.img" "$WORK/esp/EFI/UniXNU/Drivers/DarwinBoot.efi" ::/EFI/UniXNU/Drivers/
mcopy -i "$WORK/esp.img" "$WORK/esp/EFI/UniXNU/Payloads/kernel.macho" ::/EFI/UniXNU/Payloads/
mcopy -i "$WORK/esp.img" "$WORK/esp/EFI/UniXNU/Payloads/kernel.macho.adt" ::/EFI/UniXNU/Payloads/

echo "== 启动 QEMU"
pkill -9 -f qemu-system-aarch64 >/dev/null 2>&1
sleep 1
cp /usr/share/AAVMF/AAVMF_VARS.fd /tmp/vars.fd
qemu-system-aarch64 -M virt -cpu cortex-a72 -m 2048 -smp 4 \
  -drive if=pflash,format=raw,readonly=on,file=/usr/share/AAVMF/AAVMF_CODE.fd \
  -drive if=pflash,format=raw,file=/tmp/vars.fd \
  -drive if=none,id=esp,format=raw,readonly=on,file="$WORK/esp.img" \
  -device virtio-blk-pci,drive=esp -device ramfb -device qemu-xhci -device usb-kbd \
  -nic none -display none -no-reboot \
  -serial "file:$LOG" -qmp "unix:$QMP,server=on,wait=off" \
  -d in_asm -D "/tmp/${PREFIX}.inasm" &
QPID=$!

echo "== 等待内核启动（45 秒）"
sleep 45

echo
echo "===== 串口日志尾部 ====="
tail -6 "$LOG" 2>/dev/null

echo
echo "===== 内核 debug 缓冲区 ====="
python3 "$SELF/read-debug-buf.py" "$QMP" 2>&1 | head -50

echo
echo "===== 截屏 ====="
python3 "$SELF/qmp-shot.py" "$QMP" "/tmp/${PREFIX}.ppm" 2>&1 | tail -2
[[ -f "/tmp/${PREFIX}.ppm" ]] && cp "/tmp/${PREFIX}.ppm" "/mnt/e/DeepSeekWorkspace/out/r11/${PREFIX}.ppm" && echo "截屏已拷贝: out/r11/${PREFIX}.ppm"

kill -9 $QPID >/dev/null 2>&1
if [[ -f "/tmp/${PREFIX}.inasm" ]]; then
  cp "/tmp/${PREFIX}.inasm" "/mnt/e/DeepSeekWorkspace/out/r11/${PREFIX}.inasm"
  echo "轨迹已拷贝: out/r11/${PREFIX}.inasm"
fi
echo "== 完成"
