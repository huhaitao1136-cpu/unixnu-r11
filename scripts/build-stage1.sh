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

# UniXNU 的 UniXNULib 用到 gFdtTableGuid，但 INF 的 [Packages] 里没有 EmbeddedPkg。
# 给三个 UniXNU 模块统一补上（幂等）。
for UINF in \
  "$BASE/out/r11/tree/package/UniXNUPkg/Library/UniXNULib/UniXNULib.inf" \
  "$BASE/out/r11/tree/package/UniXNUPkg/Application/UniXNU/UniXNU.inf" \
  "$BASE/out/r11/tree/package/UniXNUPkg/Driver/UniXNUDxe/UniXNUDxe.inf"; do
  [[ -f "$UINF" ]] || continue
  # 先清掉历史注入的声明块（早期版本注入过 Mu 已声明的函数，会造成冲突）。
  if [[ "$UINF" == *UniXNULib.inf ]]; then
    UDIR0="$(dirname "$UINF")"
    for SRC in "$UDIR0"/*.c; do
      [[ -f "$SRC" ]] || continue
      if grep -q 'R11FdtCompatDecls' "$SRC"; then
        python3 - "$SRC" <<'PYCLEAN'
import re, sys
p = sys.argv[1]
lines = open(p, encoding="utf-8", errors="replace").read().splitlines()
out, skip = [], False
decl = re.compile(r"^(UINT16|UINT32|UINT64|INT32|CONST CHAR8 \*|CONST VOID \*)\s+Fdt\w+\s*\(")
for l in lines:
    if l.startswith("/* R11FdtCompatDecls"):
        skip = True
        continue
    if skip:
        if decl.match(l):
            continue
        if l.strip() == "":
            skip = False
            continue
        skip = False
    out.append(l)
open(p, "w", encoding="utf-8").write("\n".join(out) + "\n")
print("  cleaned ->", p.split("/")[-1])
PYCLEAN
      fi
    done
  fi

  # 本版 Mu 的 FdtLib.h 没有这些函数的声明（C99 不允许隐式声明），
  # 在 UniXNULib 的源文件里补上原型 —— 实现由 R11FdtCompat 提供。
  if [[ "$UINF" == *UniXNULib.inf ]]; then
    UDIR="$(dirname "$UINF")"
    for SRC in "$UDIR"/*.c; do
      [[ -f "$SRC" ]] || continue
      if ! grep -q 'R11FdtCompatDecls' "$SRC"; then
        python3 - "$SRC" <<'PYDECL'
import re, sys
p = sys.argv[1]
s = open(p, encoding="utf-8", errors="replace").read()
if "R11FdtCompatDecls" in s:
    raise SystemExit(0)
decls = ("\n/* R11FdtCompatDecls: Mu 的 FdtLib 缺这些函数，实现见 r11Pkg/Library/R11FdtCompat */\n"
         "INT32       FdtOpenInto (VOID *Fdt, VOID *Buffer, INT32 BufferSize);\n"
         "INT32       FdtPack (VOID *Fdt);\n"
         "CONST CHAR8 *FdtStrerror (INT32 Err);\n"
         "INT32       FdtOverlayApply (VOID *Fdt, CONST VOID *Overlay);\n"
         "INT32       FdtTotalSize (CONST VOID *Fdt);\n"
         "INT32       FdtPathOffset (CONST VOID *Fdt, CONST CHAR8 *Path);\n"
         "INT32       FdtDelProp (VOID *Fdt, INT32 NodeOffset, CONST CHAR8 *Name);\n"
         "INT32       FdtDelNode (VOID *Fdt, INT32 NodeOffset);\n")
m = list(re.finditer(r"(?m)^#include .*$", s))
if m:
    at = m[-1].end()
    s = s[:at] + "\n" + decls + s[at:]
else:
    s = decls + s
open(p, "w", encoding="utf-8").write(s)
print("  decls ->", p.split("/")[-1])
PYDECL
      fi
    done
  fi
  # 第二批：FdtPathOffset / FdtDelProp / FdtDelNode（第一块标记存在时追加）
  if [[ "$UINF" == *UniXNULib.inf ]]; then
    UDIR2="$(dirname "$UINF")"
    for SRC in "$UDIR2"/*.c; do
      [[ -f "$SRC" ]] || continue
      if ! grep -q 'FdtPathOffset (CONST VOID' "$SRC"; then
        python3 - "$SRC" <<'PYDECL2'
import re, sys
p = sys.argv[1]
s = open(p, encoding="utf-8", errors="replace").read()
if "FdtPathOffset (CONST VOID" in s:
    raise SystemExit(0)
decls = ("\n/* R11FdtCompatDecls2 */\n"
         "INT32       FdtPathOffset (CONST VOID *Fdt, CONST CHAR8 *Path);\n"
         "INT32       FdtDelProp (VOID *Fdt, INT32 NodeOffset, CONST CHAR8 *Name);\n"
         "INT32       FdtDelNode (VOID *Fdt, INT32 NodeOffset);\n")
m = list(re.finditer(r"(?m)^#include .*$", s))
if m:
    at = m[-1].end()
    s = s[:at] + "\n" + decls + s[at:]
else:
    s = decls + s
open(p, "w", encoding="utf-8").write(s)
print("  decls2 ->", p.split("/")[-1])
PYDECL2
      fi
    done
  fi
  # 第三批：FdtGetProp
  if [[ "$UINF" == *UniXNULib.inf ]]; then
    UDIR3="$(dirname "$UINF")"
    for SRC in "$UDIR3"/*.c; do
      [[ -f "$SRC" ]] || continue
      if ! grep -q 'FdtGetProp (CONST VOID' "$SRC"; then
        python3 - "$SRC" <<'PYDECL3'
import re, sys
p = sys.argv[1]
s = open(p, encoding="utf-8", errors="replace").read()
if "FdtGetProp (CONST VOID" in s:
    raise SystemExit(0)
decls = ("\n/* R11FdtCompatDecls3 */\n"
         "CONST VOID *FdtGetProp (CONST VOID *Fdt, INT32 NodeOffset, CONST CHAR8 *Name, INT32 *Length);\n")
m = list(re.finditer(r"(?m)^#include .*$", s))
at = m[-1].end() if m else 0
s = s[:at] + "\n" + decls + s[at:]
open(p, "w", encoding="utf-8").write(s)
print("  decls3 ->", p.split("/")[-1])
PYDECL3
      fi
    done
  fi
  # UniXNULib 还需要 R11FdtCompatLib（Mu 的 FdtLib 缺 5 个函数）
  if [[ "$UINF" == *UniXNULib.inf ]] && ! grep -q 'R11FdtCompatLib' "$UINF"; then
    python3 - "$UINF" <<'PYLIB'
import re, sys
p = sys.argv[1]
s = open(p, encoding="utf-8", errors="replace").read()
if "R11FdtCompatLib" in s:
    raise SystemExit(0)
m = re.search(r"(?m)^\[LibraryClasses\]\s*$", s)
if m:
    s = s[:m.end()] + "\n  R11FdtCompatLib" + s[m.end():]
else:
    s = s.rstrip() + "\n\n[LibraryClasses]\n  R11FdtCompatLib\n"
open(p, "w", encoding="utf-8").write(s)
print("  library: R11FdtCompatLib ->", p.split("/")[-1])
PYLIB
  fi
  if ! grep -q 'EmbeddedPkg/EmbeddedPkg.dec' "$UINF"; then
    python3 - "$UINF" <<'PYINF'
import re, sys
p = sys.argv[1]
s = open(p, encoding="utf-8", errors="replace").read()
if "EmbeddedPkg/EmbeddedPkg.dec" in s:
    raise SystemExit(0)
m = re.search(r"(?m)^\[Packages\]\s*$", s)
if m:
    s = s[:m.end()] + "\n  EmbeddedPkg/EmbeddedPkg.dec" + s[m.end():]
else:
    s = s.rstrip() + "\n\n[Packages]\n  MdePkg/MdePkg.dec\n  EmbeddedPkg/EmbeddedPkg.dec\n"
open(p, "w", encoding="utf-8").write(s)
print("  patched", p.split("/")[-1])
PYINF
  fi
done

# UniXNULog 的级别与 DxeCore 的 INFO 撞车，把 UniXNU 的日志抬到 ERROR，
# 这样配合全局掩码 0x80000000 就只剩 UNIXNU: 行。
# UniXNU 源码补丁（日志走 ConOut、跳过 ConnectAll/Watchdog、分步标记）
python3 "$SELF/patch-unixnu.py" "$BASE/out/r11/tree/package" || echo "   !! patch-unixnu 失败"

UDEC="$BASE/out/r11/tree/package/UniXNUPkg/UniXNUPkg.dec"
if [[ -f "$UDEC" ]]; then
  sed -i "s/0x00000040/0x80000000/" "$UDEC"
fi

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
PAYLOAD_LZ="$PAYLOAD_IMG.lzma"
RAMDISK_ARGS=(--ramdisk none)
if [[ -f "$PAYLOAD_IMG" ]]; then
  # R11 的 ABL 对 ramdisk 有 32 MiB 上限，而 payload 有 ~56 MiB。
  # 压缩成 LZMA-Alone（固件用 LzmaCustomDecompressLib 解压），约 10-15 MB。
  # 方案 A：把编好的 UniXNU.efi 放进卷里（\EFI\BOOT\BOOTAA64.EFI），
  # 固件按文件路径加载它，而不是从 FV 里取 PE32 缓冲区。
  UAPP_EFI="$MU/Build/r11Pkg/DEBUG_CLANGDWARF/AARCH64/UniXNUPkg/Application/UniXNU/UniXNU/OUTPUT/UniXNU.efi"
  if [[ -f "$UAPP_EFI" ]]; then
    echo "==> 把 UniXNU.efi 写入 payload 卷"
    python3 "$SELF/embed-unixnu-app.py" "$PAYLOAD_IMG" "$UAPP_EFI" \
      || echo "   !! 写入失败（继续，固件会退回 FV 加载）"
  else
    echo "   !! 没找到 $UAPP_EFI（固件将退回 FV 加载）"
  fi

  echo "==> 压缩 payload（LZMA）"
  python3 - "$PAYLOAD_IMG" "$PAYLOAD_LZ" <<'PYLZ'
import lzma, sys
data = open(sys.argv[1], 'rb').read()
packed = bytearray(lzma.compress(data, format=lzma.FORMAT_ALONE, preset=6))
# Python 把"解压后大小"写成 0xFFFFFFFFFFFFFFFF（未知）；
# EDK2 的 LzmaDecompress 需要真实长度，回填进去。
packed[5:13] = len(data).to_bytes(8, 'little')
open(sys.argv[2], 'wb').write(packed)
print(f'    payload {len(data)} B -> {len(packed)} B (LZMA)')
if len(packed) > 32 * 1024 * 1024:
    raise SystemExit('!! 压缩后仍超过 ramdisk 的 32 MiB 上限')
PYLZ
  RAMDISK_ARGS=(--ramdisk "$PAYLOAD_LZ")
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
