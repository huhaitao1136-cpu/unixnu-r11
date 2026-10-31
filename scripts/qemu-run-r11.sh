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
PSIGNAL_ADDR=0xfffffff0076ef7d0      # _psignal_internal

# 上面这些是手抄的旧地址，内核一换就漂。能拿到符号表时以符号为准：
#   R11_SYMS=<kernel-symbols.txt> 或默认 ../out/r11/kernel/kernel-symbols.txt
R11_SYMS=${R11_SYMS:-/root/src/UniXNU/out/r11/kernel/kernel-symbols.txt}
sym_addr() {
  [ -f "$R11_SYMS" ] || return 1
  awk -v s="$1" '$NF==s {print "0x"$1; exit}' "$R11_SYMS" | grep -q . || return 1
  awk -v s="$1" '$NF==s {print "0x"$1; exit}' "$R11_SYMS"
}
A=$(sym_addr _load_machfile)      && [ -n "$A" ] && LOADMACH_ADDR=$A
A=$(sym_addr _exec_mach_imgact)   && [ -n "$A" ] && MACH_IMGACT_ADDR=$A
A=$(sym_addr _panic)              && [ -n "$A" ] && PANIC_ADDR=$A
# 想在任意位置停下来看寄存器：R11_BP=0xfffffff0076d4b58（load_machfile 返回后第一条）
R11_BP=${R11_BP:-}
if [ -n "${R11_HBREAK:-}" ]; then R11_BPT=hbreak; else R11_BPT=break; fi
R11_GDB_DELAY=${R11_GDB_DELAY:-20}

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
    sleep "${R11_GDB_DELAY}"
    if [ -n "$R11_BP" ]; then
      # 只停在指定地址，把 lo32 和全 64 位一起打出来，便于判断是不是指针状野值。
      "$GB" -batch -nx -ex "set pagination off" -ex "set confirm off" \
        -ex "target remote :1234" \
        -ex "$R11_BPT *$R11_BP" \
        -ex "continue" \
        -ex 'printf "=== 停在 %s ===\n", '$R11_BP'' \
        -ex 'info registers x0 x1 x2 x3 x19 x24 x29 x30' \
        -ex 'x/2i $pc-8' \
        2>&1
    else
      "$GB" -batch -nx -ex "set pagination off" -ex "set confirm off" \
        -ex "target remote :1234" \
        -ex "$R11_BPT *$LOADMACH_ADDR" \
        -ex "continue" \
        -ex 'printf "=== 进入 load_machfile ===\n"' \
        -ex 'info registers x0 x1 x30' \
        -ex "$R11_BPT *$x30" \
        -ex "continue" \
        -ex 'printf "=== load_machfile 返回 ===\n"' \
        -ex 'info registers x0' \
        2>&1
    fi
  ) > "/tmp/${PREFIX}.exec.log" 2>&1 &
fi

# ---- CATCH_SIG：只抓发给 init 的 SIGKILL(9)，看是谁发的 ----
if [ -n "${CATCH_SIG:-}" ]; then
  (
    sleep "${R11_GDB_DELAY}"
    "$GB" -batch -nx -ex "set pagination off" -ex "set confirm off" \
      -ex "target remote :1234" \
      -ex "$R11_BPT *$PSIGNAL_ADDR" \
      -ex "condition 1 \$x4 == 9" \
      -ex "continue" \
      -ex 'printf "=== 捕获 SIGKILL 发送点 ===\n"' \
      -ex 'info registers x0 x1 x2 x3 x4 x5 x30' \
      -ex 'printf "--- os_reason 结构（x5）---\n"' \
      -ex 'x/8wx $x5' \
      -ex 'bt 8' \
      2>&1
  ) > "/tmp/${PREFIX}.sig.log" 2>&1 &
fi

if [ -n "${CATCH_PANIC:-}" ]; then
  (
    sleep "${R11_GDB_DELAY}"
    "$GB" -batch -nx -ex "set pagination off" -ex "set confirm off" \
      -ex "target remote :1234" \
      -ex "$R11_BPT *$PANIC_ADDR" -ex "continue" \
      -ex 'printf "=== hit _panic ===\n"' \
      -ex 'info registers x0 x1 x2 x3 x4' \
      -ex 'x/s $x0' \
      -ex 'x/s $x1' \
      -ex 'x/s $x2' \
      -ex 'x/12gx $sp' 2>&1
  ) > "/tmp/${PREFIX}.panic.log" 2>&1 &
fi

echo "== 等待内核启动（${R11_WAIT:-45} 秒）"
sleep "${R11_WAIT:-45}"

echo
echo "===== 串口日志尾部 ====="
tail -8 "$LOG" 2>/dev/null

echo
echo "===== 内核 debug 缓冲区 ====="
if [ -S "$QMP" ]; then python3 "$SELF/read-debug-buf.py" "$QMP" 2>&1 | head -20; else echo "(QMP socket 不在，跳过)"; fi

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

if [ -n "${CATCH_SIG:-}" ]; then
  echo
  echo "===== SIGKILL 捕获 ====="
  grep -av '^warning' "/tmp/${PREFIX}.sig.log" 2>/dev/null | head -40
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
