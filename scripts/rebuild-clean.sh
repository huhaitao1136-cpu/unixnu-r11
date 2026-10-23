#!/usr/bin/env bash
# rebuild-clean.sh — 一键干净重建 R11 阶段 1 引导镜像
#
# 用法（在 WSL 里）：
#   bash /mnt/e/DeepSeekWorkspace/unixnu-r11/scripts/rebuild-clean.sh
#
# 它会：同步两个仓库 → 打印关键配置 → 清空构建产物 → 全量重编 → 校验 → 打 boot.img
set -uo pipefail

E=/mnt/e/DeepSeekWorkspace
BASE=/root/src/UniXNU
MU=/root/src/Mu-Silicium
UE=/root/src/uefi-r11
UNX=/root/src/unixnu-r11

echo "==== 1) 同步仓库 ===="
cp -a "$E/uefi-r11/."    "$UE/"
cp -a "$E/unixnu-r11/."  "$UNX/"
echo "  ok"

echo "==== 1.5) 生成 payload FAT12 镜像 ===="
mkdir -p "$UE/Platforms/OPPO/r11Pkg/Resources/Payload"
python3 "$UNX/scripts/make-payload-fat.py" "$UE/Platforms/OPPO/r11Pkg/Resources/Payload/payload.fat"
ls -l "$UE/Platforms/OPPO/r11Pkg/Resources/Payload/payload.fat"

echo "==== 2) 关键配置检查 ===="
echo -n "  DXE.inc 里 TimerDxe      : "
grep -E '^\s*INF ArmPkg/Drivers/TimerDxe' "$UE/Platforms/OPPO/r11Pkg/Include/DXE.inc" || echo "（未启用）"
echo -n "  APRIORI.inc 里 TimerDxe  : "
grep -E '^\s*INF ArmPkg/Drivers/TimerDxe' "$UE/Platforms/OPPO/r11Pkg/Include/APRIORI.inc" || echo "（未启用）"
echo -n "  DisplayDxe 引用          : "
grep -cE '^\s*INF Binaries/r11/QcomPkg/Drivers/DisplayDxe' "$UE/Platforms/OPPO/r11Pkg/Include/DXE.inc" || true
echo -n "  ConOut 行列              : "
grep -E 'PcdConOut(Column|Row)' "$UE/Platforms/OPPO/r11Pkg/r11.dsc" | sed 's/^ *//' | tr '\n' ' '
echo ""
echo -n "  MipiFrameBuffer          : "
grep -E 'PcdMipiFrameBuffer(Width|Height|ColorDepth)' "$UE/Platforms/OPPO/r11Pkg/r11.dsc" | sed 's/^ *//' | tr '\n' ' '
echo ""

echo "==== 本次开关: SKIP_MUPATCH=${SKIP_MUPATCH:-1}  R11_FB_MARK=${R11_FB_MARK:-0} ===="
echo "==== 3) 清空构建产物 ===="
rm -rf "$MU/Build/r11Pkg" "$BASE/out/r11/boot"
echo "  ok"

echo "==== 4) 全量重编（5–10 分钟）===="
cd "$UNX" || exit 1
SKIP_MUPATCH=${SKIP_MUPATCH:-1} R11_FB_MARK=${R11_FB_MARK:-0} MU="$MU" UE="$UE" BASE="$BASE" ./scripts/build-stage1.sh 2>&1 | tee /tmp/r11-build.log | \
  grep -E "BootShim|自检|FD =|boot.img|error|错误|失败|!!"

rc=${PIPESTATUS[0]}

# ---- 第二遍：把刚编出来的 payload 应用塞进 FAT，再重打一次 FD ----
APP_EFI=$(find "$MU/Build/r11Pkg/${TARGET:-DEBUG}_CLANGDWARF/AARCH64/Platforms/OPPO/r11Pkg/Applications" \
               -name 'R11PayloadTest.efi' 2>/dev/null | head -1)
if [[ -n "$APP_EFI" && -f "$APP_EFI" ]]; then
  echo "==== 4.5) 把 payload 应用打进 FAT：$APP_EFI ===="
  python3 "$UNX/scripts/make-payload-fat.py" \
      "$UE/Platforms/OPPO/r11Pkg/Resources/Payload/payload.fat" \
      --add "EFI/BOOT/BOOTAA64.EFI=$APP_EFI"
  echo "==== 4.6) 第二次构建（FD 里换成带应用的 FAT）===="
  SKIP_MUPATCH=1 R11_FB_MARK=0 MU="$MU" UE="$UE" BASE="$BASE" ./scripts/build-stage1.sh 2>&1 | \
    tee /tmp/r11-build2.log | grep -E "BootShim|自检|FD =|boot.img|error|错误|失败|!!"
  rc=${PIPESTATUS[0]}
else
  echo "!! 没找到 R11PayloadTest.efi，跳过第二遍（payload 里不会有启动程序）"
fi

echo "==== 构建退出码: $rc ===="
if [[ $rc -ne 0 ]]; then
  echo "---- 日志尾部 ----"
  tail -30 /tmp/r11-build.log
  exit $rc
fi

echo "==== 5) 产物校验 ===="
ls -l "$MU/BootShim/BootShim.S" "$BASE/out/r11/boot/boot.img" 2>/dev/null
echo -n "  涂屏标记在 BootShim.S 里 : "
grep -c r11_mark "$MU/BootShim/BootShim.S" || true
echo -n "  boot.img sha256          : "
sha256sum "$BASE/out/r11/boot/boot.img" 2>/dev/null | cut -d' ' -f1

echo "==== 完成：把 boot.img 拷到 Windows 后先别刷，等确认 ===="
echo "  cp $BASE/out/r11/boot/boot.img $E/out/r11/boot/r11-mark2.img"
