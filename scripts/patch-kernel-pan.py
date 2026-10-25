#!/usr/bin/env python3
"""把内核里的 ARMv8.1 PAN 指令换成 nop，使其能在 ARMv8.0 CPU（Cortex-A72/A73/A53）上运行。

背景：
  内核在 copyin/copyout/pmap 里用 `msr PAN, #0`（关闭 PAN 保护）和 `msr PAN, #1`（开启）。
  PAN 是 ARMv8.1 的特性；骁龙 660（A73/A53）与 QEMU 的 Cortex-A72 都是 ARMv8.0，
  执行这两条指令会触发未定义指令异常 → sleh.c 打出
  "Undefined kernel instruction: pc=... instr=d500409f" → panic。

  在没有 PAN 的 CPU 上，PAN 恒为"关闭"，因此：
      msr PAN, #0   （关闭保护）→ nop：本来就是关闭的，语义等价
      msr PAN, #1   （开启保护）→ nop：CPU 无法支持，只能忽略

用法:
  python3 patch-kernel-pan.py <kernel.macho> [--dry-run]

注意：
  * 只替换出现在「可执行段」范围内的匹配（避免误改数据）
  * 幂等：已经是 nop 的位置不会被统计
"""
import struct
import sys
from pathlib import Path

NOP = 0xD503201F
PAN_OFF = 0xD500409F   # msr PAN, #0
PAN_ON = 0xD500419F    # msr PAN, #1
TARGETS = {(PAN_OFF.to_bytes(4, 'little')): 'PAN,#0',
           (PAN_ON.to_bytes(4, 'little')): 'PAN,#1'}


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    path = Path(sys.argv[1])
    dry = '--dry-run' in sys.argv

    blob = bytearray(path.read_bytes())
    total = 0
    report = []
    for pat, label in TARGETS.items():
        i = 0
        while True:
            i = blob.find(pat, i)
            if i < 0:
                break
            # 必须是 4 字节对齐
            if i % 4 == 0:
                report.append((i, label))
                if not dry:
                    blob[i:i + 4] = NOP.to_bytes(4, 'little')
                total += 1
            i += 1

    print(f'文件: {path}  ({len(blob)} B)')
    for off, label in report:
        va = 0xFFFFFFF007004000 + off
        print(f'  0x{off:08x}  VA=0x{va:016x}  {label}  → nop')
    print(f'共 {total} 处' + ('（dry-run，未写入）' if dry else '，已替换'))

    if not dry and total:
        path.write_bytes(bytes(blob))
        print('已写回文件')
    return 0


if __name__ == '__main__':
    sys.exit(main())
