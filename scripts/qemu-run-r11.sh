#!/usr/bin/env bash
# QEMU 调试环：跑一次 R11 内核，打印串口/调试缓冲/截屏/轨迹。
# 用法: bash qemu-run-r11.sh [内核] [前缀] [initrd]
# 环境: CATCH_PANIC=1 断 _panic; CATCH_EXEC=1 断 load_machfile
set -u

KERNEL="${1:-/root/src/UniXNU/out/r11/kernel/kernel.macho}"
PREFIX="${2:-run}"
INITRD="${3:-}"
SELF="$(cd "$(dirname "$0")" && pwd)"
MU=/root/src/Mu-Silicium/Build/r11Pkg/DEBUG_CLANGDWARF/AARCH64
APP="$MU/UniXNUPkg/Application/UniXNU/UniXNU/OUTPUT/UniXNU.efi"
DB="$MU/DarwinBootPkg/Driver/DarwinBoot/OUTPUT/DarwinBoot.efi"
WORK=/tmp/r11qemu
ADT=/tmp/qemu.adt
LOG=/tmp/${PREFIX}.log
QMP=/tmp/${PREFIX}-qmp.sock
PANIC_ADDR=0xfffffff007974aac
HDR_LOAD=0xfffffff0076cca08             # ldr x24,[x0,#0x40] 取头指针
FILETYPE_CHK=0xfffffff0076cca78         # filetype 检查
MAC_EXEC_ADDR=0xfffffff00794aecc        # _mac_vnode_check_exec
MACH_IMGACT_ADDR=0xfffffff0076cc9d0    # _exec_mach_imgact
LOADMACH_ADDR=0xfffffff00775a8a8

fail() { echo "!! $*"; exit 1; }

for f in "$APP" "$DB" "$KERNEL" "$ADT"; do
  [[ -f "$f" ]] || fail "缺少 $f"
done
[[ -z "$INITRD" || -f "$INITRD" ]] || fail "缺少 initrd $INITRD"

echo "== 拼 ESP"
rm -rf "$WORK/esp"
if [[ -n "$INITRD" ]]; then
  python3 "$SELF/make-qemu-esp.py" --out "$WORK/esp" --unixnu-app "$APP" --darwinboot "$DB" --kernel "$KERNEL" --adt "$ADT" --initrd "$INITRD" >/dev/null || fail "make-qemu-esp 失败"
else
  python3 "$SELF/make-qemu-esp.py" --out "$WORK/esp" --unixnu-app "$APP" --darwinboot "$DB" --kernel "$KERNEL" --adt "$ADT" >/dev/null || fail "make-qemu-esp 失败"
fi

echo "== 做 FAT32 镜像"
rm -f "$WORK/esp.img"
dd if=/dev/zero of="$WORK/esp.img" bs=1M count=260 status=none || fail "dd 失败"
mkfs.vfat -F 32 "$WORK/esp.img" >/dev/null || fail "mkfs.vfat 失败"
mmd -i "$WORK/esp.img" ::/EFI ::/EFI/BOOT ::/EFI/UniXNU ::/EFI/UniXNU/Drivers ::/EFI/UniXNU/Payloads
mcopy -i "$WORK/esp.img" "$WORK/esp/EFI/BOOT/BOOTAA64.EFI" ::/EFI/BOOT/
mcopy -i "$WORK/esp.img" "$WORK/esp/EFI/UniXNU/unixnu.toml" ::/EFI/UniXNU/
mcopy -i "$WORK/esp.img" "$WORK/esp/EFI/UniXNU/Drivers/DarwinBoot.efi" ::/EFI/UniXNU/Drivers/
mcopy -i "$WORK/esp.img" "$WORK/esp/EFI/UniXNU/Payloads/kernel.macho" ::/EFI/UniXNU/Payloads/
mcopy -i "$WORK/esp.img" "$WORK/esp/EFI/UniXNU/Payloads/kernel.macho.adt" ::/EFI/UniXNU/Payloads/
if [[ -n "$INITRD" ]]; then
  mcopy -i "$WORK/esp.img" "$WORK/esp/EFI/UniXNU/Payloads/scratch-data.img" ::/EFI/UniXNU/Payloads/ || fail "拷 initrd 失败"
fi

echo "== 启动 QEMU"
pkill -9 -f qemu-system-aarch64 >/dev/null 2>&1
sleep 1
rm -f "$LOG"
cp /usr/share/AAVMF/AAVMF_VARS.fd /tmp/vars.fd
qemu-system-aarch64 -M virt -cpu cortex-a72 -m 4096 -smp 4 \
  -drive if=pflash,format=raw,readonly=on,file=/usr/share/AAVMF/AAVMF_CODE.fd \
  -drive if=pflash,format=raw,file=/tmp/vars.fd \
  -drive if=none,id=esp,format=raw,readonly=on,file="$WORK/esp.img" \
  -device virtio-blk-pci,drive=esp -device ramfb \
  -nic none -display none -no-reboot -s \
  -serial "file:$LOG" -qmp "unix:$QMP,server=on,wait=off" \
  -d in_asm -D "/tmp/${PREFIX}.inasm" &
QPID=$!

GB=$(command -v gdb-multiarch || command -v gdb)

if [ -n "${CATCH_EXEC:-}" ]; then
  (
    sleep 3
    "$GB" -batch -nx -ex "set pagination off" -ex "set confirm off" \
      -ex "target remote :1234" \
      -ex "hbreak *$HDR_LOAD" \
      -ex "continue" \
      -ex 'printf "=== 命中 exec_mach_imgact 头加载点 ===\n"' \
      -ex 'info registers x0' \
      -ex 'set $h = *(unsigned long *)($x0 + 0x40)' \
      -ex 'printf "头缓冲区指针 = %p\n", $h' \
      -ex 'x/32bx $h' \
      -ex 'x/8wx $h' \
      -ex 'printf "--- imgp 结构（找 ip_arch_size=0x10000）---\n"' \
      -ex 'x/96gx $x0' \
      -ex 'printf "--- ip_vp 是文件还是目录？---\n"' \
      -ex 'set $vp = *(unsigned long *)($x0 + 0x20)' \
      -ex 'printf "ip_vp = %p\n", $vp' \
      -ex 'x/32gx $vp' \
      2>&1
  ) > "/tmp/${PREFIX}.exec.log" 2>&1 &
fi

if [ -n "${CATCH_PANIC:-}" ]; then
  (
    sleep 3
    "$GB" -batch -nx -ex "set pagination off" -ex "set confirm off" \
      -ex "target remote :1234" \
      -ex "hbreak *$PANIC_ADDR" -ex "continue" \
      -ex 'printf "=== hit _panic ===\n"' \
      -ex 'info registers x0 x1 x2 x3 x4' \
      -ex 'x/s $x0' -ex 'x/12gx $sp' 2>&1
  ) > "/tmp/${PREFIX}.panic.log" 2>&1 &
fi

echo "== 等待内核启动（45 秒）"
sleep 45

echo
echo "===== 串口日志尾部 ====="
tail -8 "$LOG" 2>/dev/null

echo
echo "===== 内核 debug 缓冲区 ====="
python3 "$SELF/read-debug-buf.py" "$QMP" 2>&1 | head -20

if [ -n "${CATCH_PANIC:-}" ]; then
  echo
  echo "===== _panic 捕获日志 ====="
  grep -av '^warning' "/tmp/${PREFIX}.panic.log" 2>/dev/null | head -30
fi

if [ -n "${CATCH_EXEC:-}" ]; then
  echo
  echo "===== load_machfile 捕获 ====="
  grep -av '^warning' "/tmp/${PREFIX}.exec.log" 2>/dev/null | head -70
fi

echo
echo "===== 截屏 ====="
python3 "$SELF/qmp-shot.py" "$QMP" "/tmp/${PREFIX}.ppm" 2>&1 | tail -2
[[ -f "/tmp/${PREFIX}.ppm" ]] && cp "/tmp/${PREFIX}.ppm" "/mnt/e/DeepSeekWorkspace/out/r11/${PREFIX}.ppm" && echo "截屏已拷贝"

kill -9 $QPID >/dev/null 2>&1
if [[ -f "/tmp/${PREFIX}.inasm" ]]; then
  cp "/tmp/${PREFIX}.inasm" "/mnt/e/DeepSeekWorkspace/out/r11/${PREFIX}.inasm"
  echo "轨迹已拷贝"
fi
echo "== 完成"
