#!/usr/bin/env bash
# 在 QEMU 里读内核某个虚拟地址的内容（gdb 走 MMU，地址是虚拟地址）。
# 用法: bash gdb-read-addr.sh <va> [等待秒] [端口]
set -u
VA="${1:-0xFFFFFFF007AC6970}"
WAIT="${2:-45}"
PORT="${3:-1236}"

pkill -9 -x qemu-system-aarch64 >/dev/null 2>&1
sleep 1
cp /usr/share/AAVMF/AAVMF_VARS.fd /tmp/vars.fd

qemu-system-aarch64 -M virt -cpu cortex-a72 -m 2048 -smp 4 \
  -drive if=pflash,format=raw,readonly=on,file=/usr/share/AAVMF/AAVMF_CODE.fd \
  -drive if=pflash,format=raw,file=/tmp/vars.fd \
  -drive if=none,id=esp,format=raw,readonly=on,file=/tmp/r11qemu/esp.img \
  -device virtio-blk-pci,drive=esp -device ramfb -device qemu-xhci -device usb-kbd \
  -nic none -display none -no-reboot \
  -serial file:/tmp/grd.log -gdb "tcp::$PORT" &
QPID=$!

echo "== 等 $WAIT 秒"
sleep "$WAIT"

timeout 90 gdb-multiarch -q -batch \
  -ex 'set architecture aarch64' \
  -ex "target remote :$PORT" \
  -ex "printf \"=== 0x$VA 处的 32 个 8 字节 ===\\n\"" \
  -ex "x/32gx $VA" \
  -ex "printf \"=== 同为 4 字节视图 ===\\n\"" \
  -ex "x/32wx $VA" \
  2>&1 | tail -40

kill -9 "$QPID" >/dev/null 2>&1
echo "== 完成"
