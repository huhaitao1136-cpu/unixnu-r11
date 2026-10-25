#!/usr/bin/env python3
"""修补 UniXNU 的 build-native-aes-bindings.py。

上游这个脚本有两个问题（在 CI 的全新检出上必然失败）：
  1. 它把 original-bindings.o 写到 build/software-aes/，但不创建这个目录
  2. 它需要的 NativeAES.o 由 build-arm-driver.py 生成在 build/native-aes/，
     脚本不会去那里取

本脚本在 `args = parser.parse_args()` 之后插入一小段代码：
  - mkdir -p 输出目录
  - 如果输出目录里没有 NativeAES.o，就从 build/native-aes/ 复制过来

用法:
  python3 fix-native-aes-bindings.py <path/to/build-native-aes-bindings.py>
"""
import sys
from pathlib import Path

MARK = "# R11_PATCH_MKDIR"

INJECT = '''
# R11_PATCH_MKDIR: 上游脚本没有创建输出目录，也没有把 build/native-aes/NativeAES.o 拷过来
_out = args.objects_dir
_out.mkdir(parents=True, exist_ok=True)
_src = Path(__file__).resolve().parents[1] / 'build' / 'native-aes' / 'NativeAES.o'
if (not (_out / 'NativeAES.o').exists()) and _src.exists():
    (_out / 'NativeAES.o').write_bytes(_src.read_bytes())
'''


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    p = Path(sys.argv[1])
    if not p.is_file():
        print(f'{p} 不存在，跳过')
        return 0
    s = p.read_text()
    if MARK in s:
        print('已打过补丁')
        return 0
    anchor = 'args = parser.parse_args()'
    if anchor not in s:
        print('找不到锚点 args = parser.parse_args()')
        return 1
    s = s.replace(anchor, anchor + INJECT, 1)
    p.write_text(s)
    print('已插入 mkdir/拷贝补丁')
    return 0


if __name__ == '__main__':
    sys.exit(main())
