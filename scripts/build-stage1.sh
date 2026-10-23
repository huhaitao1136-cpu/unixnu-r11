#!/usr/bin/env bash
# build-stage1.sh — R11 阶段 1：合并 uefi-r11 到 Mu-Silicium → 编 UEFI 固件 → 打 boot.img
#
# 用法：
#   MU=~/src/Mu-Silicium UE=~/src/uefi-r11 BASE=~/src/UniXNU ./scripts/build-stage1.sh [DEBUG|RELEASE]
#
# 产物：
#   $MU/Build/r11Pkg/<TARGET>_CLANGDWARF/FV/R11_UEFI.fd
#   $BASE/out/r11/boot/boot.img
#
# 脚本里所有 Mu 补丁都是幂等的：跑多少次结果一样。
set -euo pipefail
export GIT_TERMINAL_PROMPT=0

# ---------- 参数 ----------
SELF=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
BASE=${BASE:-$HOME/src/UniXNU}
UNX=${UNX:-$(cd "$SELF/.." && pwd)}
UE=${UE:-$(cd "$SELF/../.." && pwd)/uefi-r11}
MU=${MU:-$HOME/src/Mu-Silicium}
export UNIXNU_TOOLCHAIN=${UNIXNU_TOOLCHAIN:-CLANGDWARF}
TARGET=${TARGET:-DEBUG}
if [[ $# -ge 1 && ( "$1" == "DEBUG" || "$1" == "RELEASE" ) ]]; then TARGET=$1; fi
export TARGET
SKIP_MUPATCH=${SKIP_MUPATCH:-0}   # 设为 1 则不应用 Mu 源码补丁

echo "  BASE        = $BASE"
echo "  UNX         = $UNX"
echo "  UE          = $UE"
echo "  MU          = $MU"
echo "  TARGET      = $TARGET"
echo "  TOOLCHAIN   = $UNIXNU_TOOLCHAIN"

for d in "$BASE" "$UNX" "$UE" "$MU"; do
  [[ -d $d ]] || { echo "!! 目录不存在: $d" >&2; exit 1; }
done
[[ -f "$UNX/board.toml" ]] || { echo "!! $UNX/board.toml 缺失" >&2; exit 1; }

EDK2="$MU/Mu_Basecore"
CONF="$BASE/out/r11/edk2-conf"

# ---------- 1) 合并平台包 ----------
echo "==> 合并 r11Pkg / r11.conf / r11.toml / Binaries/r11"
mkdir -p "$MU/Platforms/OPPO" "$MU/Resources/Configs" "$MU/Binaries"
rm -rf "$MU/Platforms/OPPO/r11Pkg" "$MU/Binaries/r11"
cp -a "$UE/Platforms/OPPO/r11Pkg" "$MU/Platforms/OPPO/"
cp -a "$UE/Binaries/r11" "$MU/Binaries/"
for cfg in r11.conf r11.toml; do
  if [[ -f "$UE/Resources/Configs/$cfg" ]]; then
    cp -a "$UE/Resources/Configs/$cfg" "$MU/Resources/Configs/"
  fi
done

# ---------- 2) 行尾归一化 ----------
echo "==> 归一化行尾"
find "$MU/Platforms/OPPO/r11Pkg" "$MU/Binaries/r11" -type f \
     \( -name '*.dsc' -o -name '*.fdf' -o -name '*.dec' -o -name '*.inc' \
        -o -name '*.c' -o -name '*.h' -o -name '*.inf' -o -name '*.conf' \
        -o -name '*.toml' -o -name '*.py' -o -name '*.cfg' \) \
     -exec sed -i 's/\r$//' {} + 2>/dev/null || true
for cfg in r11.conf r11.toml; do
  [[ -f "$MU/Resources/Configs/$cfg" ]] && sed -i 's/\r$//' "$MU/Resources/Configs/$cfg"
done

# ---------- 3) Mu 补丁：关掉 KEP ----------
KEP_INC="$MU/Silicon/Qualcomm/SDM660Pkg/SDM660Pkg.dsc.inc"
if [[ -f "$KEP_INC" ]] && grep -qE '^[[:space:]]*ENABLE_KEP[[:space:]]*=[[:space:]]*1' "$KEP_INC"; then
  sed -i -E 's/^(  ENABLE_KEP[[:space:]]*=[[:space:]]*)1/\10/' "$KEP_INC"
  echo "==> 已关闭 ENABLE_KEP"
fi

# ---------- 4) Mu 补丁：TimerDxe 缺 GIC 时优雅返回 ----------
TD="$MU/Silicon/Arm/Mu_Tiano/ArmPkg/Drivers/TimerDxe/TimerDxe.c"
if [[ $SKIP_MUPATCH != 1 && -f "$TD" ]] && ! grep -q 'TimerDxe: HardwareInterrupt protocol not found' "$TD"; then
  python3 - "$TD" <<'PYEOF'
import sys
p = sys.argv[1]
s = open(p, encoding="utf-8", errors="replace").read()
old = ("  Status = gBS->LocateProtocol (&gHardwareInterruptProtocolGuid, NULL, (VOID **)&gInterrupt);\n"
       "  ASSERT_EFI_ERROR (Status);")
new = ("  Status = gBS->LocateProtocol (&gHardwareInterruptProtocolGuid, NULL, (VOID **)&gInterrupt);\n"
       "  if (EFI_ERROR (Status)) {\n"
       '    DEBUG ((DEBUG_ERROR, "TimerDxe: HardwareInterrupt protocol not found; skipping timer\\n"));\n'
       "    return Status;\n"
       "  }")
if old in s:
    open(p, "w", encoding="utf-8").write(s.replace(old, new, 1))
    print("==> TimerDxe.c: 缺 GIC 时改为优雅返回")
else:
    print("!! TimerDxe.c: 锚点未找到（可能已打过补丁）")
PYEOF
fi

# ---------- 5) Mu 补丁：内存保护设置清零 ----------
if [[ $SKIP_MUPATCH != 1 ]]; then
python3 - "$MU/Mu_Basecore/MdeModulePkg" <<'PYEOF'
import re, sys
from pathlib import Path
root = Path(sys.argv[1])
cands = list(root.rglob("DxeMemoryProtectionHobLib.c")) if root.is_dir() else []
if not cands:
    print("!! 找不到 DxeMemoryProtectionHobLib.c，跳过")
    raise SystemExit(0)
f = cands[0]
t = f.read_text(encoding="utf-8", errors="replace")
if "r11: memory protection settings forcibly zeroed" in t:
    print("==> 内存保护补丁：已存在")
    raise SystemExit(0)
m = re.search(r"DxeMemoryProtectionHobLibConstructor\s*\([^)]*\)\s*\{", t, re.S)
if not m:
    print("!! 构造函数签名没匹配上，跳过")
    raise SystemExit(0)
inject = ("\n  // r11: memory protection settings forcibly zeroed (bring-up)\n"
          "  ZeroMem (&gDxeMps, sizeof (gDxeMps));\n"
          "  return EFI_SUCCESS;\n")
f.write_text(t[:m.end()] + inject + t[m.end():], encoding="utf-8")
print("==> 内存保护补丁：已注入 HobLib 构造函数")
PYEOF
fi

# ---------- 6) Mu_Basecore + 嵌套子模块 ----------
if [[ ! -f "$EDK2/MdePkg/MdePkg.dec" ]]; then
  echo "==> 初始化 Mu_Basecore 子模块"
  git -C "$MU" submodule update --init --depth 1 Mu_Basecore
fi
[[ -f "$EDK2/MdePkg/MdePkg.dec" ]] || { echo "!! Mu_Basecore 不完整: $EDK2" >&2; exit 1; }

need_nested=0
for p in "$EDK2/MdePkg/Library/MipiSysTLib/mipisyst/library/include" \
         "$MU/Common/Mu_Tiano_Plus/SecurityPkg/DeviceSecurity/SpdmLib/libspdm/include"; do
  if [[ -z "$(ls -A "$p" 2>/dev/null)" ]]; then need_nested=1; fi
done
if [[ $need_nested -eq 1 ]]; then
  echo "==> 递归初始化 Mu-Silicium 子模块"
  git -C "$MU" submodule update --init --recursive --depth 1 -- \
      Common/Mu Common/Mu_OEM_Sample Common/Mu_Tiano_Plus Features/DFCI \
      Silicon/Arm/Mu_Tiano Mu_Basecore 2>&1 | tail -5 || true
fi

# ---------- 7) 清掉旧 VFR 产物与 edk2-conf ----------
rm -rf "$CONF"
if [[ -d "$MU/Build/r11Pkg" ]]; then
  n=$(find "$MU/Build/r11Pkg" \( -name '*Vfr.i' -o -name '*Vfr.c' -o -name '*Vfr.obj' \) 2>/dev/null | wc -l)
  find "$MU/Build/r11Pkg" \( -name '*Vfr.i' -o -name '*Vfr.c' -o -name '*Vfr.obj' \) -delete 2>/dev/null || true
  if [[ $n -gt 0 ]]; then echo "==> 清掉 $n 个 VFR 中间产物"; fi
fi

# ---------- 8) BootShim 适配 + 执行标记 ----------
# 除了宏名适配，还在 _Start 开头插入“把 splash framebuffer 涂白”的代码。
# 目的：UEFI 本身不输出任何东西时，也能从屏幕上看出 BootShim 是否被执行过。
BSRC="$MU/BootShim/AARCH64/BootShim.S"
BDST="$MU/BootShim/BootShim.S"
if [[ -f "$BSRC" ]]; then
  python3 - "$BSRC" "$BDST" "${R11_FB_MARK:-1}" <<'PYEOF'
import re, sys

src, dst, mark = sys.argv[1], sys.argv[2], sys.argv[3]
lines = open(src, encoding="utf-8").read().splitlines()

out = [
    "/* auto-generated by build-stage1.sh */",
    "#define UEFI_BASE            FD_BASE",
    "#define UEFI_SIZE            FD_SIZE",
    "#define CREATE_FDT_POINTER   0",
    "#define FDT_POINTER_ADDRESS  0x0",
]

MARK = """  /* r11 marker: paint the splash framebuffer white so we can see
     whether BootShim executed, even when UEFI prints nothing */
  ldr   x20, =0x9D400000
  ldr   x22, =0x9DC00000
  movz  x21, #0xFFFF
  movk  x21, #0x00FF, lsl #16
r11_mark:
  stp   x21, x21, [x20], #16
  cmp   x20, x22
  b.lo  r11_mark
"""

inserted = False
for ln in lines:
    if ln.strip().startswith("_Start:"):
        out.append(ln)
        if mark == "1":
            out.append(MARK)
            inserted = True
        continue
    # ARM64 header: image_size 必须覆盖 shim + 固件卷
    ln = re.sub(
        r"^(\s*\.quad\s+)UEFI_SIZE(\s*)(/\*.*\*/\s*)?$",
        r"\1UEFI_SIZE + (_Payload - _Head)\2\3",
        ln,
    )
    out.append(ln)

open(dst, "w", encoding="utf-8").write("\n".join(out) + "\n")
print(f"==> BootShim.S: 已生成（涂屏标记={'开' if inserted else '关'}）")
PYEOF
fi

# ---------- 8.5) 装配构建树，并修补 DarwinBoot 的 INF ----------
# DarwinBoot 的 Entry.S 需要 ArmPkg 的 AsmMacroLib.h，但它的 INF 里没有
# ArmPkg/ArmPkg.dec，导致 -I 路径缺失。这里在装配好的 tree 里补上。
echo "==> 装配构建树（unixnu tree）"
( cd "$BASE" && ./unixnu tree -D "$UNX" ) >/dev/null 2>&1 || true

# DarwinBoot 的 Entry.S 用的是新版 edk2 的头名 AsmMacroLib.h，
# 这个 Mu 版本里叫 AsmMacroIoLibV8.h —— 改掉它。
# DarwinBoot 的 Entry.S 用的是新版 edk2 的头名 AsmMacroLib.h，
# 这个 Mu 版本里叫 AsmMacroIoLibV8.h。直接改子模块源文件（tree 每次装配都会重新链接它），
# 同时把已经装配好的 tree 副本也改一遍。
for DBDIR in "$BASE/drivers/DarwinBoot" "$BASE/out/r11/tree/drivers/DarwinBoot"; do
  [[ -d "$DBDIR" ]] || continue
  find "$DBDIR" -name '*.S' -type f 2>/dev/null | while IFS= read -r f; do
    sed -i 's|#include <AsmMacroLib.h>|#include <AsmMacroIoLibV8.h>|' "$f"
  done
  echo "  DarwinBoot: .S 头文件已适配（$DBDIR）"
done

DBINF="$BASE/out/r11/tree/drivers/DarwinBoot/DarwinBootPkg/Driver/DarwinBoot.inf"
if [[ -f "$DBINF" ]] && ! grep -q 'ArmPkg/ArmPkg.dec' "$DBINF"; then
  python3 - "$DBINF" <<'PYEOF'
import re, sys
p = sys.argv[1]
s = open(p, encoding="utf-8", errors="replace").read()
if "ArmPkg/ArmPkg.dec" in s:
    raise SystemExit(0)
m = re.search(r"(?m)^\[Packages\]\s*$", s)
if m:
    s = s[:m.end()] + "\n  ArmPkg/ArmPkg.dec" + s[m.end():]
else:
    s = s.rstrip() + "\n\n[Packages]\n  MdePkg/MdePkg.dec\n  ArmPkg/ArmPkg.dec\n"
open(p, "w", encoding="utf-8").write(s)
print("  DarwinBoot.inf: 已补 ArmPkg/ArmPkg.dec")
PYEOF
fi

# ---------- 9) 编固件 ----------
chmod +x "$BASE/unixnu"
echo "==> unixnu build firmware ($TARGET)"
set +e
( cd "$BASE" && ./unixnu build -D "$UNX" firmware --uefi "$MU" --edk2 "$EDK2" --target "$TARGET" ) \
  2>&1 | tee "$SELF/../docs/build-firmware.log"
rc=${PIPESTATUS[0]}
set -e
if [[ $rc -ne 0 ]]; then
  echo "!! firmware 构建失败（rc=$rc），日志：docs/build-firmware.log" >&2
  exit $rc
fi

# ---------- 10) 定位 FD + 自检 ----------
FVDIR="$MU/Build/r11Pkg/${TARGET}_${UNIXNU_TOOLCHAIN}/FV"
FD="$FVDIR/R11_UEFI.fd"
if [[ ! -f $FD ]]; then FD="$FVDIR/r11_UEFI.fd"; fi
if [[ ! -f $FD ]]; then
  FD=$(find "$FVDIR" -maxdepth 1 -name '*_UEFI.fd' -print -quit 2>/dev/null || true)
fi
if [[ ! -f $FD ]]; then
  echo "!! 找不到 *UEFI.fd（工具链 $UNIXNU_TOOLCHAIN）" >&2
  exit 1
fi

size=$(stat -c%s "$FD")
printf '    FD = %s  (%d B = 0x%x)\n' "$FD" "$size" "$size"
if (( size > 0x300000 )); then
  echo "!! 固件卷超过 TARGET_FD_SIZE 0x300000" >&2
  exit 1
fi
if command -v strings >/dev/null 2>&1; then
  cnt() { { strings    "$FD"; strings -el "$FD"; } 2>/dev/null | grep -c -i "$1" || true; }
  echo "    自检 ArmTimerDxe: $(cnt ArmTimerDxe) 处（ASCII+UTF16）"
  echo "    自检 DisplayDxe : $(cnt DisplayDxe) 处（ASCII+UTF16）"
fi

# ---------- 11) 打 boot.img ----------
DTB="$UE/DeviceTree/r11.dtb"
if [[ ! -f "$DTB" ]]; then DTB=""; fi

# UniXNU payload（kernel.macho + unixnu.toml）由 "unixnu build payload" 产出。
# 有就作为 ramdisk 打进 boot.img；没有就退回空 ramdisk（阶段 1/2 的验证模式）。
PAYLOAD_IMG="$BASE/out/r11/payload/payload.img"
RAMDISK_ARGS=(--ramdisk none)
if [[ -f "$PAYLOAD_IMG" ]]; then
  PAYLOAD_SIZE=$(stat -c%s "$PAYLOAD_IMG")
  RAMDISK_ARGS=(--ramdisk "$PAYLOAD_IMG")
  echo "==> 使用 payload 作为 ramdisk: $PAYLOAD_IMG ($PAYLOAD_SIZE B)"
else
  echo "==> 没找到 payload.img，使用空 ramdisk"
fi

echo "==> unixnu bootimg ${DTB:+--dtb $DTB}"
( cd "$BASE" && ./unixnu bootimg -D "$UNX" --uefi "$MU" --fd "$FD" "${RAMDISK_ARGS[@]}" ${DTB:+--dtb "$DTB"} ) \
  2>&1 | tee "$SELF/../docs/build-bootimg.log"

IMG="$BASE/out/r11/boot/boot.img"
if [[ ! -f $IMG ]]; then
  echo "!! 没产出 $IMG" >&2
  exit 1
fi
printf '    boot.img = %s (%d B)\n' "$IMG" "$(stat -c%s "$IMG")"

echo ""
echo "==> 完成。Windows 侧："
echo "    copy 上面这个 boot.img 到 E:\\DeepSeekWorkspace\\out\\r11\\boot\\"
echo "    fastboot flash boot <那个路径>"
echo "    fastboot reboot"
echo "    回滚：fastboot flash boot E:\\DeepSeekWorkspace\\R11-backup\\firmware\\boot.img"
