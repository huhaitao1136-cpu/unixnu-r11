#!/usr/bin/env python3
"""把 payload.img 重建成更大的卷，并放入稠密的 APFS Data seed。

步骤：
  1. 从旧 payload 里抽出所有文件到暂存目录
  2. 用新的 UUID 重新生成 ADT（--data-ramdisk，即 XNU 认识的 "RAMDisk"）
  3. 用新的 seed.raw 替换 Payloads/scratch-data.img
  4. mformat 一个 size_mib 大小（默认 128 MiB）的新 FAT 卷，把暂存目录拷回去

用法:
  python3 repack-payload-bigger.py --payload payload.img --seed seed.raw --uuid UUID [--size-mib 128]
"""
import argparse
import re
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path

BOARD = Path(__file__).resolve().parents[1] / 'board.toml'
DEFAULT_ADT = Path('/root/src/UniXNU/out/r11/tree/scripts/make-r11-adt.py')


def run(cmd, quiet=False):
    r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True)
    if r.returncode != 0:
        print('!! 失败:', ' '.join(str(c) for c in cmd))
        print((r.stderr or r.stdout)[:800])
    elif not quiet:
        for line in (r.stdout or '').strip().splitlines()[:10]:
            print('   ', line)
    return r


def find_prop(blob: bytes, name: str):
    key = name.encode() + b'\0'
    idx = blob.find(key[:32])
    while idx >= 0:
        if blob[idx:idx + len(key)] == key:
            length = struct.unpack_from('<I', blob, idx + 32)[0]
            if 0 < length <= 64:
                return blob[idx + 36: idx + 36 + length]
        idx = blob.find(key[:32], idx + 1)
    return None


def parse_board():
    s = BOARD.read_text(encoding='utf-8')
    def grab(pat, default=None):
        m = re.search(pat, s)
        return m.group(1) if m else default
    return {
        'dram_size': grab(r'^\s*dram_size\s*=\s*"?([^"\n]+)"?', '0xFE4C0000'),
        'owned': grab(r'^\s*owned_memory_size\s*=\s*"?([^"\n]+)"?', '0x60000000'),
        'chip_id': grab(r'^\s*chip_id\s*=\s*"?([^"\n]+)"?', '207'),
        'cpu_regs': grab(r'^\s*cpu_regs\s*=\s*"([^"]+)"', '0,0x1,0x2,0x3,0x100,0x101,0x102,0x103'),
        'psci': grab(r'^\s*psci_method\s*=\s*"([^"]+)"', 'smc'),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--payload', required=True, type=Path)
    ap.add_argument('--seed', required=True, type=Path)
    ap.add_argument('--uuid', required=True)
    ap.add_argument('--size-mib', type=int, default=128)
    ap.add_argument('--adt-tool', type=Path, default=DEFAULT_ADT)
    a = ap.parse_args()

    for f in (a.payload, a.seed, a.adt_tool):
        if not Path(f).is_file():
            print(f'找不到 {f}')
            return 1

    tmp = Path(tempfile.mkdtemp(prefix='r11repack-'))
    stage = tmp / 'stage'
    stage.mkdir()

    print('== 抽出旧 payload 内容')
    if run(['mcopy', '-s', '-n', '-Q', '-i', a.payload, '::/*', stage]).returncode != 0:
        return 1
    for p in sorted(stage.rglob('*')):
        if p.is_file():
            print(f'   {p.relative_to(stage)}  {p.stat().st_size}')

    payloads = stage / 'EFI/UniXNU/Payloads'
    toml = stage / 'EFI/UniXNU/unixnu.toml'
    if not payloads.is_dir() or not toml.is_file():
        print('!! payload 目录结构不符合预期')
        return 1

    old_adt = payloads / 'kernel.macho.adt'
    blob = old_adt.read_bytes()
    chip = find_prop(blob, 'unique-chip-id')
    if chip is None:
        print('!! 旧 ADT 里找不到 unique-chip-id')
        return 1
    chip_hex = chip.hex().upper()
    print(f'== unique-chip-id = {chip_hex}')

    board = parse_board()
    new_adt = payloads / 'kernel.macho.adt.new'
    print('== 重新生成 ADT（--data-ramdisk，稠密镜像）')
    cmd = [
        'python3', a.adt_tool, '--output', new_adt,
        '--dram-size', str(board['dram_size']),
        '--owned-memory-size', str(board['owned']),
        '--root-match-volume-uuid', a.uuid,
        '--chip-id', str(board['chip_id']),
        '--cpu-regs', board['cpu_regs'],
        '--psci-method', board['psci'],
        '--unique-chip-id', chip_hex,
        '--data-ramdisk',
        '--persistent-data',
        '--ipad-identity',
    ]
    if run(cmd).returncode != 0:
        return 1
    if a.uuid.encode() not in new_adt.read_bytes():
        print('!! 新 ADT 里没有 UUID')
        return 1
    new_adt.replace(old_adt)
    print('   ADT 已替换')

    print('== 放入稠密 seed')
    old_seed = payloads / 'scratch-data.img'
    if old_seed.exists():
        old_seed.unlink()
    shutil.copyfile(a.seed, old_seed)
    print(f'   scratch-data.img = {old_seed.stat().st_size} B')

    text = toml.read_text(encoding='utf-8', errors='replace')
    if 'initrd' not in text:
        text = text.replace('path = "Payloads/kernel.macho"\n',
                            'path = "Payloads/kernel.macho"\ninitrd = "Payloads/scratch-data.img"\n', 1)
        toml.write_text(text, encoding='utf-8')
        print('   toml 已加 initrd')
    else:
        print('   toml 已有 initrd')

    total = sum(p.stat().st_size for p in stage.rglob('*') if p.is_file())
    print(f'== 内容合计 {total >> 20} MiB，新卷 {a.size_mib} MiB')

    new_img = tmp / 'payload-new.img'
    sectors = a.size_mib * 2048
    print('== mformat 新卷')
    if run(['mformat', '-C', '-F', '-T', str(sectors), '-v', 'R11ESP', '-N', '53484e47',
            '-i', new_img, '::']).returncode != 0:
        return 1

    print('== 拷回文件')
    if run(['mcopy', '-s', '-Q', '-m', '-i', new_img, stage / 'EFI', '::/']).returncode != 0:
        return 1

    print('== 目录检查')
    run(['mdir', '-i', new_img, '::/EFI/UniXNU/Payloads'])

    backup = a.payload.with_suffix('.img.bak')
    shutil.copyfile(a.payload, backup)
    print(f'== 旧 payload 备份到 {backup}')
    shutil.copyfile(new_img, a.payload)
    print(f'== 新 payload: {a.payload} ({a.payload.stat().st_size >> 20} MiB)')
    print('完成。接下来重新打 boot.img。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
