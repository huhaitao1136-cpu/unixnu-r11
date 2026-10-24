#!/usr/bin/env python3
"""给 DarwinBoot 的 ReadBootVideo 加"无 GOP 时的帧缓冲兜底"。

我们的固件没有 SimpleFbDxe（没有 GOP），所以手机侧 boot_args->Video 全是 0，
内核拿不到帧缓冲 → 绿条画不出来、panic 屏幕也画不出来。

补丁：GOP 找不到时用 Display Reserved 区的固定参数填 Video[6]：
  Video[0] = 0x9D400000   framebuffer base
  Video[1] = 1            display
  Video[2] = 1080*4       row bytes
  Video[3] = 1080         width
  Video[4] = 1920         height
  Video[5] = 32           depth

用法:
  python3 patch-darwinboot-video.py [/root/src/UniXNU/drivers/DarwinBoot/DarwinBootPkg/Driver/DarwinBoot.c]
"""
import sys
from pathlib import Path

DEFAULT = "/root/src/UniXNU/drivers/DarwinBoot/DarwinBootPkg/Driver/DarwinBoot.c"

OLD = "  if (Status == EFI_NOT_FOUND) { return EFI_SUCCESS; }"

NEW = """  if (Status == EFI_NOT_FOUND) {
    /* R11：本固件没有 GOP（没有 SimpleFbDxe），用 Display Reserved 区的固定参数
       兜底，这样内核能拿到有效帧缓冲，绿条和 panic 屏幕才画得出来。 */
    Video[0] = 0x9D400000ULL;
    Video[1] = 1;
    Video[2] = 1080ULL * 4;
    Video[3] = 1080;
    Video[4] = 1920;
    Video[5] = 32;
    return EFI_SUCCESS;
  }"""


def main() -> int:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else DEFAULT)
    if not path.is_file():
        print(f'!! 找不到 {path}')
        return 1
    s = path.read_text(encoding="utf-8")
    if "0x9D400000ULL" in s:
        print("已经打过补丁")
        return 0
    if OLD not in s:
        print("!! 锚点没匹配（代码版本不同？）")
        hits = [l for l in s.splitlines() if 'EFI_NOT_FOUND' in l and 'return EFI_SUCCESS' in l]
        print("候选行:", hits)
        return 1
    s = s.replace(OLD, NEW, 1)
    path.write_text(s, encoding="utf-8", newline="\n")
    print(f"已给 {path.name} 加帧缓冲兜底")
    for line in s.splitlines():
        if '0x9D400000ULL' in line:
            print("  ", line.strip())
    return 0


if __name__ == '__main__':
    sys.exit(main())
