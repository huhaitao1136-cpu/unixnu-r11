#!/usr/bin/env python3
"""本地给 payload 加 Data seed（不需要 macOS / CI）。

流程：
  1. 从 payload.img 里取出 kernel.macho.adt，读出 unique-chip-id 与 chip-id
  2. 用 make-r11-adt.py 重新生成 ADT：
        --packed-data-ramdisk --root-match-volume-uuid <UUID> --persistent-data --ipad-identity
  3. 把 seed 拷进 payload 作为 EFI/UniXNU/Payloads/scratch-data.img
  4. 改 unixnu.toml：加上 initrd = "Payloads/scratch-data.img"

用法:
  python3 local-add-data-seed.py --payload payload.img --seed seed.dbr --uuid XXXX-...
                             [--adt-tool /root/src/UniXNU/out/r11/tree/scripts/make-r11-adt.py]

之后重新打 boot.img 并刷机即可。
"""
import argparse
import re
import struct
import subprocess
import tempfile
from pathlib import Path

BOARD = Path(__file__).resolve().parents[1] / 'board.toml'


def run(cmd, quiet=False):
    r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True)
    if r.returncode != 0:
        print('!! 失败:', ' '.join(str(c) for c in cmd))
        print((r.stderr or r.stdout)[:600])
    elif not quiet:
        for line in (r.stdout or '').strip().splitlines()[:8]:
            print('   ', line)
    return r


def find_prop(blob: bytes, name: str):
    """在 ADT 里找属性：键名 32 字节，随后 4 字节长度（小端），再是数据。"""
    key = name.encode() + b'\0'
    idx = blob.find(key[:32])
    while idx >= 0:
        head = blob[idx:idx + 36]
        if head[:len(key)] == key:
            length = struct.unpack_from('<I', blob, idx + 32)[0]
            if 0 < length <= 64:
                return blob[idx + 36: idx + 36 + length]
        idx = blob.find(key[:32], idx + 1)
    return None


def parse_board():
    """从 board.toml 里抠出 ADT 相关参数（简单正则，够用）。"""
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
    ap.add_argument('--adt-tool', type=Path,
                    default=Path('/root/src/UniXNU/out/r11/tree/scripts/make-r11-adt.py'))
    a = ap.parse_args()

    if not a.payload.is_file():
        print(f'payload 不存在: {a.payload}')
        return 1
    if not a.seed.is_file():
        print(f'seed 不存在: {a.seed}')
        return 1
    if not a.adt_tool.is_file():
        print(f'ADT 生成脚本不存在: {a.adt_tool}')
        return 1

    tmp = Path(tempfile.mkdtemp(prefix='r11seed-'))
    adt_in = tmp / 'old.adt'
    adt_out = tmp / 'new.adt'

    print('== 取出旧 ADT')
    if run(['mcopy', '-o', '-i', a.payload, '::/EFI/UniXNU/Payloads/kernel.macho.adt', adt_in]).returncode != 0:
        return 1
    blob = adt_in.read_bytes()

    chip_id = find_prop(blob, 'unique-chip-id')
    if chip_id is None:
        print('!! 旧 ADT 里找不到 unique-chip-id，需要手动传 --unique-chip-id')
        return 1
    chip_hex = chip_id.hex().upper()
    print(f'   unique-chip-id = {chip_hex}')

    board = parse_board()
    print(f'   board: {board}')

    print('== 重新生成 ADT（带 Data seed）')
    cmd = [
        'python3', a.adt_tool,
        '--output', adt_out,
        '--dram-size', str(board['dram_size']),
        '--owned-memory-size', str(board['owned']),
        '--root-match-volume-uuid', a.uuid,
        '--chip-id', str(board['chip_id']),
        '--cpu-regs', board['cpu_regs'],
        '--psci-method', board['psci'],
        '--unique-chip-id', chip_hex,
        '--packed-data-ramdisk',
        '--persistent-data',
        '--ipad-identity',
    ]
    if run(cmd).returncode != 0:
        return 1
    if not adt_out.is_file():
        print('!! ADT 没有生成')
        return 1

    # 验证：新 ADT 里的 UUID 对不对
    nb = adt_out.read_bytes()
    if a.uuid.encode() not in nb:
        print('!! 新 ADT 里没有出现 UUID，检查参数名')
        return 1
    print('   ADT 已包含 UUID ✓')

    print('== 写入 payload')
    if run(['mcopy', '-o', '-i', a.payload, str(a.seed), '::/EFI/UniXNU/Payloads/scratch-data.img']).returncode != 0:
        return 1
    if run(['mcopy', '-o', '-i', a.payload, str(adt_out), '::/EFI/UniXNU/Payloads/kernel.macho.adt']).returncode != 0:
        return 1

    print('== 修改 unixnu.toml（加 initrd）')
    toml_in = tmp / 'unixnu.toml'
    if run(['mcopy', '-o', '-i', a.payload, '::/EFI/UniXNU/unixnu.toml', toml_in]).returncode != 0:
        return 1
    text = toml_in.read_text(encoding='utf-8', errors='replace')
    if 'initrd' not in text:
        text = text.replace('path = "Payloads/kernel.macho"\n',
                            'path = "Payloads/kernel.macho"\ninitrd = "Payloads/scratch-data.img"\n', 1)
        toml_in.write_text(text, encoding='utf-8')
        print('   已插入 initrd')
    if run(['mcopy', '-o', '-i', a.payload, str(toml_in), '::/EFI/UniXNU/unixnu.toml']).returncode != 0:
        return 1

    print('== 目录检查')
    run(['mdir', '-i', a.payload, '::/EFI/UniXNU/Payloads'])
    print('完成。接下来重新打 boot.img 并刷机。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
