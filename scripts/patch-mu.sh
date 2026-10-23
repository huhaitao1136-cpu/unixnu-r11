#!/usr/bin/env bash
# patch-mu.sh — 直接给 Mu 源码打两个 bring-up 补丁，并立刻验证是否真的写进去了
#
# 用法：
#   bash /mnt/e/DeepSeekWorkspace/unixnu-r11/scripts/patch-mu.sh
set -uo pipefail
MU=${MU:-/root/src/Mu-Silicium}

echo "==== 补丁 1/2：TimerDxe 缺 GIC 时优雅返回 ===="
TD="$MU/Silicon/Arm/Mu_Tiano/ArmPkg/Drivers/TimerDxe/TimerDxe.c"
if [[ ! -f $TD ]]; then
  echo "!! 找不到 $TD"
else
  python3 - "$TD" <<'PYEOF'
import sys
p = sys.argv[1]
s = open(p, encoding="utf-8", errors="replace").read()
if "TimerDxe: HardwareInterrupt protocol not found" in s:
    print("  已打过，跳过"); raise SystemExit(0)
old = ("  Status = gBS->LocateProtocol (&gHardwareInterruptProtocolGuid, NULL, (VOID **)&gInterrupt);\n"
       "  ASSERT_EFI_ERROR (Status);")
new = ("  Status = gBS->LocateProtocol (&gHardwareInterruptProtocolGuid, NULL, (VOID **)&gInterrupt);\n"
       "  if (EFI_ERROR (Status)) {\n"
       '    DEBUG ((DEBUG_ERROR, "TimerDxe: HardwareInterrupt protocol not found; skipping timer\\n"));\n'
       "    return Status;\n"
       "  }")
if old not in s:
    print("  !! 锚点没找到"); raise SystemExit(1)
open(p, "w", encoding="utf-8").write(s.replace(old, new, 1))
print("  已注入")
PYEOF
fi
echo -n "  验证: "; grep -c "TimerDxe: HardwareInterrupt protocol not found" "$TD" 2>/dev/null || echo 0

echo "==== 补丁 2/2：DxeMemoryProtectionHobLib 构造函数清零 ===="
HOB="$MU/Mu_Basecore/MdeModulePkg/Library/MemoryProtectionHobLib/DxeMemoryProtectionHobLib.c"
if [[ ! -f $HOB ]]; then
  echo "!! 找不到 $HOB"
else
  python3 - "$HOB" <<'PYEOF'
import re, sys
p = sys.argv[1]
s = open(p, encoding="utf-8", errors="replace").read()
if "r11: memory protection settings forcibly zeroed" in s:
    print("  已打过，跳过"); raise SystemExit(0)
m = re.search(r"DxeMemoryProtectionHobLibConstructor\s*\([^)]*\)\s*\{", s, re.S)
if not m:
    print("  !! 构造函数锚点没找到"); raise SystemExit(1)
inject = ("\n  /* r11: memory protection settings forcibly zeroed (bring-up) */\n"
          "  ZeroMem (&gDxeMps, sizeof (gDxeMps));\n"
          "  return EFI_SUCCESS;\n")
open(p, "w", encoding="utf-8").write(s[:m.end()] + inject + s[m.end():])
print("  已注入")
PYEOF
fi
echo -n "  验证: "; grep -c "r11: memory protection settings forcibly zeroed" "$HOB" 2>/dev/null || echo 0


echo "==== 补丁 3/3：Mmu.c 遇到未知页属性不要停机 ===="
MMU="$MU/Silicon/Arm/Mu_Tiano/ArmPkg/Drivers/CpuDxe/AArch64/Mmu.c"
if [[ ! -f $MMU ]]; then
  echo "!! 找不到 $MMU"
else
  python3 - "$MMU" <<'PYEOF'
import sys
p = sys.argv[1]
s = open(p, encoding="utf-8", errors="replace").read()
if "r11: do not halt on unknown page attribute" in s:
    print("  已打过，跳过"); raise SystemExit(0)
old = """      ASSERT (0);
      // The Global Coherency Domain (GCD) value is defined as a bit set."""
new = """      /* r11: do not halt on unknown page attribute during bring-up */
      // The Global Coherency Domain (GCD) value is defined as a bit set."""
if old not in s:
    print("  !! 锚点没找到"); raise SystemExit(1)
open(p, "w", encoding="utf-8").write(s.replace(old, new, 1))
print("  已注入")
PYEOF
fi
echo -n "  验证: "; grep -c "r11: do not halt on unknown page attribute" "$MMU" 2>/dev/null || echo 0


echo "==== 补丁 4/4：DebugLib 容忍 NULL 格式串 ===="
# 厂商 DXE（NpaDxe 等）会调用 DEBUG ((level, NULL))，Mu 的 DebugLib 用
# ASSERT (Format != NULL) 把它当成致命错误。把所有实例里的这句断言中和掉。
python3 - "$MU/Mu_Basecore/MdePkg/Library" <<'PYEOF'
import re, sys
from pathlib import Path

root = Path(sys.argv[1])
pat = re.compile(r"ASSERT \(Format != NULL\);")
total = 0
for f in sorted(root.rglob("DebugLib.c")):
    t = f.read_text(encoding="utf-8", errors="replace")
    if "r11: tolerate NULL format" in t:
        print(f"  {f.parent.name}: 已打过")
        continue
    n = len(pat.findall(t))
    if n:
        t = pat.sub("/* r11: tolerate NULL format from vendor DXEs */", t)
        f.write_text(t, encoding="utf-8")
        print(f"  {f.parent.name}: 中和 {n} 处")
        total += n
print(f"  合计 {total} 处")
PYEOF
echo -n "  验证: "; grep -rc "r11: tolerate NULL format" "$MU/Mu_Basecore/MdePkg/Library"/*/DebugLib.c 2>/dev/null | grep -v ':0' | wc -l

echo "==== 完成。四行验证都应该是 1 ===="
