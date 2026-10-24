#!/usr/bin/env bash
# 让 QEMU 正常启动到内核运行，然后用 gdb 在 _panic 入口下硬件断点，
# 抓取 x0（panic 格式串地址）和现场寄存器。
#
# 用法: bash gdb-catch-panic.sh [内核] [gdb端口] [等待秒数]
set -u

KERNEL="${1:-/tmp/kernel-pan5.macho}"
PORT="${2:-1234}"
WAIT="${3:-30}"

# DarwinBoot 打印的加载信息（每次跑可能不同，从日志里读更稳，这里用默认值）
TEXT_VMADDR=0xFFFFFFF007004000
LOAD_VA=0xFFFFFFF00427C000
LOAD_PA=0x4807C000
PANIC_VA=0xFFFFFFF007974AAC      # _panic

echo "== 启动 QEMU（不带 -S，让它自己跑起来）"
pkill -9 -f qemu-system-aarch64 >/dev/null 2>&1
sleep 1
cp /usr/share/AAVMF/AAVMF_VARS.fd /tmp/vars.fd
qemu-system-aarch64 -M virt -cpu cortex-a72 -m 2048 -smp 4 \
  -drive if=pflash,format=raw,readonly=on,file=/usr/share/AAVMF/AAVMF_CODE.fd \
  -drive if=pflash,format=raw,file=/tmp/vars.fd \
  -drive if=none,id=esp,format=raw,readonly=on,file=/tmp/r11qemu/esp.img \
  -device virtio-blk-pci,drive=esp -device ramfb -device qemu-xhci -device usb-kbd \
  -nic none -display none -no-reboot \
  -serial file:/tmp/gdbcatch.log -gdb "tcp::$PORT" -S &
QPID=$!

echo "== QEMU 已暂停，直接挂 gdb（-S）"

PANIC_PA=$(python3 - "$TEXT_VMADDR" "$LOAD_VA" "$LOAD_PA" "$PANIC_VA" <<'PY'
import sys
text, lva, lpa, sym = (int(x, 0) for x in sys.argv[1:5])
slide = text - lva
print(hex((sym - slide) - (lva - lpa)))
PY
)
echo "== _panic 物理地址: $PANIC_PA"

echo "== gdb 下硬件断点并等待命中"
timeout 180 gdb-multiarch -q -batch -ex 'set confirm off' \
  -ex 'set architecture aarch64' \
  -ex "target remote :$PORT" \
  -ex "hbreak *$PANIC_PA" \
  -ex 'interrupt' \
  -ex 'c' \
  -ex 'printf "===== 命中 _panic =====\n"' \
  -ex 'info registers pc x0 x1 x2 x3 lr sp' \
  -ex 'x/6i $pc' \
  -ex 'printf "x0 指向的字符串（panic 格式串）: 0x%lx\n", $x0' \
  -ex 'x/s $x0' \
  -ex 'printf "lr = 0x%lx (调用者)\n", $lr' \
  2>&1 | tail -30

kill -9 $QPID >/dev/null 2>&1
echo "== 完成"
