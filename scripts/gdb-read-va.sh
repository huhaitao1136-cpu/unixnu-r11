#!/usr/bin/env bash
# 用 gdb 直接读内核的**虚拟地址**（QEMU gdbstub 会做 MMU 翻译）。
# 内核启动后会把 kernelcache 重定位，物理地址不好算，但虚拟地址是稳定的。
#
# 用法: bash gdb-read-va.sh [等待秒数] [端口]
set -u
WAIT="${1:-45}"
PORT="${2:-1234}"

pkill -9 -f qemu-system-aarch64 >/dev/null 2>&1
sleep 1
cp /usr/share/AAVMF/AAVMF_VARS.fd /tmp/vars.fd
qemu-system-aarch64 -M virt -cpu cortex-a72 -m 2048 -smp 4 \
  -drive if=pflash,format=raw,readonly=on,file=/usr/share/AAVMF/AAVMF_CODE.fd \
  -drive if=pflash,format=raw,file=/tmp/vars.fd \
  -drive if=none,id=esp,format=raw,readonly=on,file=/tmp/r11qemu/esp.img \
  -device virtio-blk-pci,drive=esp -device ramfb -device qemu-xhci -device usb-kbd \
  -nic none -display none -no-reboot \
  -serial file:/tmp/gdbrd.log -gdb "tcp::$PORT" &
QPID=$!

echo "== 等 $WAIT 秒让内核 panic"
sleep "$WAIT"

echo "== gdb 挂上去（会暂停 CPU）并读虚拟内存"
timeout 90 gdb-multiarch -q -batch \
  -ex 'set architecture aarch64' \
  -ex "target remote :$PORT" \
  -ex 'printf "=== 现场 ===\n"' \
  -ex 'info registers pc x1 x2 x12 x19 x26' \
  -ex 'printf "=== x1 指向的字符串 ===\n"' \
  -ex 'x/s $x1' \
  -ex 'printf "=== x12 指向的内存 ===\n"' \
  -ex 'x/8gx $x12' \
  -ex 'printf "=== x2 指向的内存 ===\n"' \
  -ex 'x/8gx $x2' \
  -ex 'printf "=== 栈顶 16 个 8 字节（可能有字符串指针） ===\n"' \
  -ex 'x/16gx $sp' \
  2>&1 | tail -40

kill -9 $QPID >/dev/null 2>&1
echo "== 完成"
