#!/usr/bin/env python3
"""把 R11 需要的所有内核补丁打进 payload 卷里的 kernel.macho。

步骤：
  1. 用 mcopy 从 payload.img 取出 EFI/UniXNU/Payloads/kernel.macho
  2. 打补丁：
       a) PAN（ARMv8.1）→ NOP / MOV #1     （骁龙 660 是 v8.0，没有 PAN）
       b) AppleARMCPU::_validateConfiguration 返回值 → 恒真
  3. mcopy 写回 payload.img

用法:
  python3 patch-payload-kernel.py <payload.img>
"""
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

NOP = struct.pack('<I', 0xD503201F)
MOV_W1 = 0x52800020                      # mov wN, #1
VALIDATE_RET = 0xFFFFFFF0049871A0        # AppleARMCPU::_validateConfiguration 的返回值判定
MOV_W0_1 = 0x52800020                    # mov w0, #1


def patch_bytes(d: bytearray) -> dict:
    stat = {'msr_pan': 0, 'mrs_pan': 0, 'illegal': 0}
    for i in range(0, len(d) - 4, 4):
        w = struct.unpack_from('<I', d, i)[0]
        if w == 0xD500419F:
            d[i:i + 4] = NOP
            stat['msr_pan'] += 1
        elif (w & 0xFFFFFFE0) == 0xD5384260:
            d[i:i + 4] = struct.pack('<I', MOV_W1 | (w & 0x1F))
            stat['mrs_pan'] += 1
        elif w == 0xE7FFDEFF:
            d[i:i + 4] = NOP
            stat['illegal'] += 1
    return stat


def segments(d: bytes):
    _, _, _, _, ncmds, _, _ = struct.unpack_from('<IiiIIII', d, 0)
    off = 32
    segs = []
    for _ in range(ncmds):
        cmd, cmdsize = struct.unpack_from('<II', d, off)
        if cmd == 0x19:
            name = d[off + 8:off + 24].rstrip(b'\0').decode(errors='replace')
            vmaddr, vmsize, fileoff, filesize = struct.unpack_from('<QQQQ', d, off + 24)
            segs.append((name, vmaddr, vmsize, fileoff, filesize))
        off += cmdsize
    return segs


def patch_validate(d: bytearray) -> bool:
    for name, vm, vs, fo, fs in segments(d):
        if vm <= VALIDATE_RET < vm + vs:
            f = fo + (VALIDATE_RET - vm)
            old = struct.unpack_from('<I', d, f)[0]
            struct.pack_into('<I', d, f, MOV_W0_1)
            print(f'  _validateConfiguration 返回值判定 0x{old:08x} -> 0x{MOV_W0_1:08x} (段 {name})')
            return True
    print('  !! 找不到 _validateConfiguration 的判定点（内核版本不同？）')
    return False




# ---------------------------------------------------------------------------
# 打开内核输出：把三个 disable_* 开关置 0，并让 serial_init() 恒返回 false，
# 使 PE_kputc 回退到 console_write_unbuffered（写帧缓冲）。
SYM_DISABLE_SERIAL  = 0xFFFFFFF0071F9B50
SYM_DISABLE_KPRINTF = 0xFFFFFFF0071F9B51
SYM_DISABLE_IOLOG   = 0xFFFFFFF0071F9B52
SYM_SERIAL_INIT     = 0xFFFFFFF00793E724
MOV_W0_0 = 0x52800000
RET      = 0xD65F03C0


def va_to_offset(d, va):
    for name, vm, vs, fo, fs in segments(d):
        if vm <= va < vm + vs:
            return fo + (va - vm), name
    return None, None


def patch_output(d: bytearray) -> dict:
    """打开 kprintf/串口输出，并让 serial_init 失败以回退到视频控制台。"""
    res = {'switches': 0, 'serial_init': False}
    for sym in (SYM_DISABLE_SERIAL, SYM_DISABLE_KPRINTF, SYM_DISABLE_IOLOG):
        off, _ = va_to_offset(d, sym)
        if off is not None:
            old = d[off]
            d[off] = 0
            res['switches'] += 1
            print(f'  输出开关 0x{sym:x}: {old} -> 0')
    off, seg = va_to_offset(d, SYM_SERIAL_INIT)
    if off is not None:
        struct.pack_into('<I', d, off, MOV_W0_0)
        struct.pack_into('<I', d, off + 4, RET)
        res['serial_init'] = True
        print(f'  serial_init 0x{sym:x} -> mov w0,#0; ret (段 {seg})')
    return res




# ARM64 上 cons_ops_index 默认是 SERIAL_CONS_OPS(=0)，输出全送到不存在的串口。
# 改成 VC_CONS_OPS(=1) 就会走视频控制台（vcputc），日志直接画到帧缓冲。
SYM_CONS_OPS_INDEX = 0xFFFFFFF007167920


def patch_console_index(d: bytearray) -> bool:
    off, seg = va_to_offset(d, SYM_CONS_OPS_INDEX)
    if off is None:
        print('  !! 找不到 cons_ops_index')
        return False
    old = struct.unpack_from('<I', d, off)[0]
    struct.pack_into('<I', d, off, 1)
    print(f'  cons_ops_index 0x{SYM_CONS_OPS_INDEX:x}: {old} -> 1 (视频控制台, 段 {seg})')
    return True


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print('!! 命令失败:', ' '.join(str(c) for c in cmd))
        print(r.stderr[:400])
    return r.returncode


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    img = Path(sys.argv[1])
    if not img.is_file():
        print(f'!! 找不到 {img}')
        return 1

    tmp = Path(tempfile.mkdtemp(prefix='r11k-'))
    src = tmp / 'kernel.macho'
    dst = tmp / 'kernel-patched.macho'

    print('== 从 payload 卷取出 kernel.macho')
    if run(['mcopy', '-o', '-i', str(img), '::/EFI/UniXNU/Payloads/kernel.macho', str(src)]):
        return 1
    print(f'   {src.stat().st_size} B')

    print('== 打补丁')
    data = bytearray(src.read_bytes())
    stat = patch_bytes(data)
    print(f'   PAN: MSR {stat["msr_pan"]} 条, MRS {stat["mrs_pan"]} 条, 非法 {stat["illegal"]} 条')
    ok = patch_validate(data)
    outp = patch_output(data)
    patch_console_index(data)
    print(f'   输出: 开关 {outp["switches"]} 个, serial_init 补丁 {"是" if outp["serial_init"] else "否"}')
    dst.write_bytes(data)

    print('== 写回 payload 卷')
    if run(['mcopy', '-o', '-i', str(img), str(dst), '::/EFI/UniXNU/Payloads/kernel.macho']):
        return 1

    print('== 校验')
    run(['mdir', '-i', str(img), '::/EFI/UniXNU/Payloads'])
    shutil.rmtree(tmp, ignore_errors=True)
    print('完成' if ok else '完成（但 CPU 校验补丁未应用）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
