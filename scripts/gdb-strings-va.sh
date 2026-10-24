#!/usr/bin/env bash
# 读 panic 附近的所有字符串 + 返回地址处的反汇编。
# 用 pkill -x（精确进程名），避免误杀自己的 shell。
set -u
WAIT="${1:-45}"
PORT="${2:-1235}"
ADDR="${3:-0xFFFFFFF004289E00}"

pkill -9 -x qemu-system-aarch64 >/dev/null 2>&1
sleep 1
cp /usr/share/AAVMF/AAVMF_VARS.fd /tmp/vars.fd

qemu-system-aarch64 -M virt -cpu cortex-a72 -m 2048 -smp 4 \
  -drive if=pflash,format=raw,readonly=on,file=/usr/share/AAVMF/AAVMF_CODE.fd \
  -drive if=pflash,format=raw,file=/tmp/vars.fd \
  -drive if=none,id=esp,format=raw,readonly=on,file=/tmp/r11qemu/esp.img \
  -device virtio-blk-pci,drive=esp -device ramfb -device qemu-xhci -device usb-kbd \
  -nic none -display none -no-reboot \
  -serial file:/tmp/g3.log -gdb "tcp::$PORT" &
QPID=$!

echo "== 等 $WAIT 秒让内核 panic"
sleep "$WAIT"

echo "== gdb 读字符串与反汇编"
timeout 90 gdb-multiarch -q -batch \
  -ex 'set architecture aarch64' \
  -ex "target remote :$PORT" \
  -ex 'info registers pc x1 x2 x30' \
  -ex "x/24s $ADDR" \
  -ex 'printf "=== 栈顶 32 个 8 字节 ===\n"' \
  -ex 'x/32gx $sp' \
  -ex 'printf "=== 返回地址附近反汇编 ===\n"' \
  -ex 'x/20i $x30' \
  2>&1 | head -80

kill -9 "$QPID" >/dev/null 2>&1
echo "== 完成"
