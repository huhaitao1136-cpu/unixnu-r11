#!/usr/bin/env python3
"""给 NativeRamBlock 生成 IOStorageFamily 的原版绑定（`.set` 到 prelink 后的绝对地址）。

为什么需要这一步
----------------
`NativeRamBlock : IOBlockStorageDevice` 继承了 Apple 原始的块设备类，而我们的内核里
只有**代码**（prelink 把 kext 段拷进来了），没有它的**符号**（`llvm-nm` 在
`kernel.macho` 上找不到任何 `IOBlockStorageDevice` 符号 —— 已经实测）。
所以链接期会缺 15 个符号（`docs/progress-r11.md` §3.44 记的就是这一堵墙）。

官方做法是 `scripts/build-native-virtio-bindings.py`：先生成一份汇编，把每个缺失符号
`.set` 成原版 kext 被 slide 之后的绝对地址，再和 `NativeRamBlock.o` 一起打成归档。
地址来自 `scripts/audit-storage-interface.py`，它需要：

  1. `refer/IOStorageFamily` —— 公开源码，用来编译出 190 槽的 vtable 顺序；
  2. `build/native-prelink/native-apfs-load-positive.macho` + `.json` —— 一个 prelink
     候选（`emit_prelink.py` 的产物），用来在原版 kext 里唯一地定位那个 vtable。

本脚本按顺序把这三步都跑掉，任何一步失败都只打印原因并退出非零，不碰内核产物。

用法（在 CI 里，工作目录为仓库根）：

    python3 device/scripts/make-ramblock-bindings.py UniXNU device/board.toml
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path


def log(text):
    print(f'[ramblock] {text}', flush=True)


def run(command, cwd, env=None):
    printable = ' '.join(str(part) for part in command)
    log(f'$ {printable}')
    result = subprocess.run(command, cwd=cwd, env=env)
    if result.returncode != 0:
        raise SystemExit(f'[ramblock] 命令失败（exit {result.returncode}）: {printable}')


def feed_dir(feeds: Path, name: str) -> Path:
    matches = sorted(feeds.glob(f'{name}-*'))
    if not matches:
        raise SystemExit(f'[ramblock] out/feeds 里没有 {name}（先跑 ./unixnu fetch）')
    return matches[-1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('repo', help='UniXNU 主仓库目录')
    parser.add_argument('board', help='board.toml 路径')
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    tree = repo / 'out' / 'r11' / 'tree'
    if not tree.is_dir():
        raise SystemExit(f'[ramblock] 找不到装配树 {tree}')

    board = tomllib.loads(Path(args.board).read_text(encoding='utf-8'))
    spec = board.get('kernel', {}).get('prelink')
    if not spec:
        raise SystemExit('[ramblock] board.toml 没有 [kernel.prelink]')

    # 1) refer/IOStorageFamily：audit 脚本会校验 pinned commit
    refer = tree / 'refer'
    refer.mkdir(exist_ok=True)
    storage = refer / 'IOStorageFamily'
    if not storage.exists():
        target = feed_dir(repo / 'out' / 'feeds', 'IOStorageFamily')
        storage.symlink_to(target)
        log(f'refer/IOStorageFamily -> {target}')

    # 2) prelink 候选（audit 的输入）。需要 noctf，即必须先成功编译过一次内核。
    noctf = tree / spec['kernel']
    if not noctf.is_file():
        raise SystemExit(f'[ramblock] 还没有内核产物 {noctf}；先跑 ./unixnu build kernel')

    kernelcache = None
    for candidate in sorted((repo / 'out' / 'ipsw').glob('*/kernelcache.decompressed')):
        kernelcache = candidate
        break
    if kernelcache is None:
        raise SystemExit('[ramblock] 找不到 out/ipsw/*/kernelcache.decompressed')

    positive = tree / 'build' / 'native-prelink' / 'native-apfs-load-positive.macho'
    positive.parent.mkdir(parents=True, exist_ok=True)
    for stale in (positive, positive.with_suffix('.json')):
        stale.unlink(missing_ok=True)

    command = [sys.executable, str(tree / spec['tool']),
               '--kernel', str(noctf), '--prelinked', str(kernelcache),
               f'--slide={spec["slide"]}', '--output', str(positive)]
    for root in spec.get('roots', []):
        command += ['--root', root]
    if spec.get('early_security_init'):
        command += ['--early-security-init', spec['early_security_init']]
    command += spec.get('extra_args', [])
    run(command, cwd=tree)
    log(f'候选: {positive} ({positive.stat().st_size} B)')

    # 3) 审计 -> build/storage-abi/interface-audit.json
    run([sys.executable, 'scripts/audit-storage-interface.py'], cwd=tree)
    audit = tree / 'build' / 'storage-abi' / 'interface-audit.json'
    if not audit.is_file():
        raise SystemExit('[ramblock] 审计没有产出 interface-audit.json')
    record = json.loads(audit.read_text())
    log(f"审计: slots={record.get('method_slots')} live4k={record.get('live_native_4k')}")

    # 4) 先确保 NativeRamBlock.o 在 build/native-ram-block 里（bindings 要链接它）
    obj = tree / 'build' / 'native-ram-block' / 'NativeRamBlock.o'
    if not obj.is_file():
        run([sys.executable, 'scripts/build-arm-driver.py', 'NativeRamBlock'], cwd=tree)

    # 5) 生成绑定归档 build/native-ram-block/libNativeRamBlock.a
    run([sys.executable, 'scripts/build-native-virtio-bindings.py',
         '--component', 'NativeRamBlock', '--directory', 'build/native-ram-block'], cwd=tree)

    lib = tree / 'build' / 'native-ram-block' / 'libNativeRamBlock.a'
    if not lib.is_file():
        raise SystemExit('[ramblock] 绑定脚本没有产出 libNativeRamBlock.a')

    symbols = subprocess.run(['nm', str(lib)], capture_output=True, text=True).stdout
    bound = sum(1 for line in symbols.splitlines() if 'IOBlockStorageDevice' in line)
    log(f'PASS: {lib} ({lib.stat().st_size} B)，IOBlockStorageDevice 相关符号 {bound} 行')
    return 0


if __name__ == '__main__':
    sys.exit(main())
