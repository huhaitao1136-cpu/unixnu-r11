#!/usr/bin/env bash
# wsl-setup.sh — 在 WSL2 (Ubuntu 24.04) 里装齐 UniXNU / Mu-Silicium 的构建依赖
#
#   sudo 权限：需要（apt）
#   用法：  ./wsl-setup.sh            # 装依赖并自检
#           ./wsl-setup.sh --qemu     # 额外装 QEMU + AAVMF（做阶段 1 的 QEMU 冒烟）
set -euo pipefail

# ubuntu-base 的 root 环境里通常没有 sudo，直接判断 EUID
if [[ ${EUID:-$(id -u)} -eq 0 ]]; then SUDO=""; else SUDO=${SUDO:-sudo}; fi
WANT_QEMU=0
SLIM=0
for arg in "$@"; do
  case $arg in
    --qemu) WANT_QEMU=1 ;;
    --slim) SLIM=1 ;;   # 不装 recommends，省 ~500 MB（个别情况要补包）
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

PKGS=(
  build-essential      # make / gcc（edk2 BaseTools 要）
  clang lld            # AARCH64 交叉编译 + 链接（BootShim / EDK2 CLANGPDB）
  llvm
  nasm                 # edk2
  python3 python3-venv # cli.py 要求 >= 3.11（24.04 自带 3.12）
  make cmake ninja-build
  mtools               # mformat/mcopy：payload FAT 卷
  device-tree-compiler # dtc / fdtput：bootimg 追加设备树
  unifdef              # libdispatch 头
  acpica-tools         # iasl：Silicium-ACPI
  uuid-dev libssl-dev bison flex
  git curl ca-certificates dos2unix
)

if [[ $WANT_QEMU -eq 1 ]]; then
  PKGS+=(qemu-system-arm qemu-efi-aarch64)
fi

echo "==> apt update"
$SUDO apt-get update
echo "==> apt install ${#PKGS[@]} packages"
if [[ $SLIM -eq 1 ]]; then
  DEBIAN_FRONTEND=noninteractive $SUDO apt-get install -y --no-install-recommends "${PKGS[@]}"
else
  DEBIAN_FRONTEND=noninteractive $SUDO apt-get install -y "${PKGS[@]}"
fi
$SUDO apt-get clean

echo
echo "==> 版本自检"
python3 - <<'PY'
import sys
if sys.version_info < (3, 11):
    sys.exit(f'unixnu needs Python >= 3.11, found {sys.version.split()[0]}')
print(f'  python  {sys.version.split()[0]}  ok')
PY

missing=0
for t in clang lld ld.lld make cmake ninja mformat mcopy dtc fdtput unifdef git; do
  if command -v "$t" >/dev/null 2>&1; then
    printf '  %-10s %s\n' "$t" "$(command -v "$t")"
  else
    printf '  %-10s MISSING\n' "$t"
    missing=1
  fi
done
if command -v iasl >/dev/null 2>&1; then printf '  %-10s %s\n' iasl "$(command -v iasl)"; else echo "  iasl       MISSING (acpica-tools)"; fi

echo
if [[ $missing -ne 0 ]]; then
  echo "!! 有工具缺失，先补齐再构建" >&2
  exit 1
fi
echo "环境就绪。下一步："
echo "  cd ~/src/unixnu-r11 && MU=~/src/Mu-Silicium UE=~/src/uefi-r11 ./scripts/build-stage1.sh"
