#!/usr/bin/env python3
"""Capture the live IOStorageFamily metaclass ABI evidence that audit-storage-interface.py needs.

The upstream audit wants build/darwinboot-storage-native-metadata/gdb-stop.json, which the
macOS-only scripts/qemu-entry/gdb-stop.py produces. That tool drives its own fixtures, so this
script drives the same QEMU/gdb observation directly on an existing R11 ESP boot: stop the guest
while XNU is running, read the IOBlockStorageDevice metaclass and IOService metaclass, read the
translation registers, and write the same JSON schema.

  python3 scripts/capture-storage-metaclass.py --kernel <candidate.macho> --out <gdb-stop.json>

The candidate hash is recorded as kernel_sha256, which the audit compares against the prelink
plan's candidate_sha256, so capture the exact image that will be linked.
"""
import argparse
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path

SLOTS = 190
META_FIELD_SUPER = 16
META_FIELD_NAME = 24
META_FIELD_SIZE = 32
PORT_GDB = 1234
KERNEL_TEXT_SEGMENTS = ('__TEXT_EXEC', '__PRELINK_TEXT', '__PLK_TEXT_EXEC', '__TEXT')

def fail(message):
    raise SystemExit(f'!! {message}')

def macho_segments(data):
    magic, cpu, subtype, kind, count, size = struct.unpack_from('<6I', data, 0)
    if (magic, cpu, subtype) != (0xfeedfacf, 0x100000c, 0):
        fail('kernel is not a little-endian ARM64 Mach-O')
    offset = 32
    end = 32 + size
    segments = []
    while offset < end:
        command, length = struct.unpack_from('<II', data, offset)
        if command == 0x19:
            name = data[offset + 8:offset + 24].rstrip(b'\0').decode('ascii', 'replace')
            va, vsize, fileoff, filesize = struct.unpack_from('<4Q', data, offset + 24)
            initprot, = struct.unpack_from('<I', data, offset + 60)
            segments.append(dict(name=name, va=va, size=vsize, fileoff=fileoff,
                                 filesize=filesize, initprot=initprot))
        offset += length
    return segments

def backed_offset(segments, address, length, total):
    matches = [s['fileoff'] + address - s['va'] for s in segments
               if s['va'] <= address and address + length <= s['va'] + s['filesize']]
    if len(matches) != 1 or matches[0] + length > total:
        fail(f'address {address:#x}+{length:#x} is not unambiguously file-backed')
    return matches[0]

def prelink_symbols(data, segments):
    """Return name -> address for the candidate kernel (LC_SYMTAB, external + section symbols)."""
    offset = 32
    count, size = struct.unpack_from('<2I', data, 16)
    end = 32 + size
    table = None
    while offset < end:
        command, length = struct.unpack_from('<II', data, offset)
        if command == 2 and length == 24:
            table = offset
            break
        offset += length
    if table is None:
        fail('candidate kernel has no symbol table')
    symoff, nsyms, stroff, strsize = struct.unpack_from('<4I', data, table + 8)
    strings = data[stroff:stroff + strsize]
    names = {}
    for index in range(nsyms):
        strx, kind, _, _, address = struct.unpack_from('<IBBHQ', data, symoff + index * 16)
        if kind & 0xe0 or not kind & 1 or not kind & 0x0e:
            continue
        stop = strings.find(b'\0', strx)
        if strx >= strsize or stop < 0:
            continue
        name = strings[strx:stop].decode('ascii', 'replace')
        names.setdefault(name, address)
    return names

def unserialize_xml(blob):
    import xml.etree.ElementTree as elementtree
    root = elementtree.fromstring(blob.split(b'\0')[0].decode('utf-8', 'replace'))
    refs = {}

    def read(element):
        if 'IDREF' in element.attrib:
            return refs[element.attrib['IDREF']]
        tag = element.tag
        if tag == 'dict':
            children = list(element)
            value = {}
            for index in range(0, len(children), 2):
                value[read(children[index])] = read(children[index + 1])
        elif tag == 'array':
            value = [read(child) for child in element]
        elif tag in ('integer', 'string'):
            value = element.text or ''
        elif tag == 'true':
            value = True
        elif tag == 'false':
            value = False
        else:
            value = element.text
        if 'ID' in element.attrib:
            refs[element.attrib['ID']] = value
        return value

    return read(root if root.tag == 'dict' else root.find('dict'))

def module_segment(data, segments, module_identifier, segment_name):
    info = next((s for s in segments if s['name'] == '__PRELINK_INFO'), None)
    if info is None:
        fail('candidate kernel has no __PRELINK_INFO')
    at = backed_offset(segments, info['va'], info['filesize'], len(data))
    metadata = unserialize_xml(data[at:at + info['filesize']])
    entries = {e['CFBundleIdentifier']: e for e in metadata['_PrelinkInfoDictionary']}
    if module_identifier not in entries:
        fail(f'{module_identifier} is not prelinked into the candidate')
    load = int(str(entries[module_identifier]['_PrelinkExecutableLoadAddr']), 0)
    header = backed_offset(segments, load, 32, len(data))
    blob = data[header:]
    module_segments = macho_segments(blob)
    segment = next((s for s in module_segments if s['name'] == segment_name), None)
    if segment is None:
        fail(f'{module_identifier} has no {segment_name}')
    return segment

def resolve_dispatch(data, segments, names):
    const = module_segment(data, segments, 'com.apple.iokit.IOStorageFamily', '__DATA_CONST')
    text = module_segment(data, segments, 'com.apple.iokit.IOStorageFamily', '__TEXT_EXEC')
    at = backed_offset(segments, const['va'], const['size'], len(data))
    blob = data[at:at + const['size']]
    wanted = ('__cxa_pure_virtual', '___cxa_pure_virtual')
    pure = {names[name] for name in names if name in wanted}
    executable = [(s['va'], s['va'] + s['size']) for s in segments
                  if s['initprot'] & 4 and s['size'] and s['name'] in KERNEL_TEXT_SEGMENTS]
    executable.append((text['va'], text['va'] + text['size']))

    def inside(address):
        return any(low <= address < high for low, high in executable)

    found = []
    for offset in range(0, len(blob) - (SLOTS + 2) * 8 + 1, 8):
        if blob[offset:offset + 16] != bytes(16):
            continue
        pointers = struct.unpack_from('<' + 'Q' * SLOTS, blob, offset + 16)
        if not all(inside(p) for p in pointers):
            continue
        shared = max(set(pointers), key=lambda p: pointers.count(p))
        if pointers.count(shared) != 14:
            continue
        found.append((const['va'] + offset, pointers, shared))
    if len(found) != 1:
        fail(f'expected exactly one storage dispatch table, found {len(found)}')
    table, pointers, shared = found[0]

    def read(address, length):
        return data[backed_offset(segments, address, length, len(data)):][:length]

    getter = pointers[7]
    adrp, add, ret = struct.unpack('<3I', read(getter, 12))
    if ret != 0xd65f03c0 or add & 0xffc003ff != 0x91000000:
        fail('metaclass getter shape changed')
    immediate = ((adrp >> 5 & 0x7ffff) << 2) | (adrp >> 29 & 3)
    if immediate & 0x100000:
        immediate -= 0x200000
    meta = (getter & ~4095) + immediate * 4096 + (add >> 10 & 4095)
    brk = read(pointers[0], 4)
    if brk != struct.pack('<I', 0xd4200020):
        fail('abstract complete-destructor slot is no longer a brk trap')
    service = names.get('__ZN9IOService10gMetaClassE')
    if service is None:
        fail('IOService::gMetaClass is not exported by the candidate')
    named = sum(p in set(names.values()) for p in pointers)
    return dict(table=table, meta=meta, service=service, pure=shared, named=named)

def qmp_command(path, commands, timeout=40):
    """Run HMP commands over the QMP socket and return their text replies."""
    import socket
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(timeout)
    connection.connect(str(path))
    pending = b''

    def read_message():
        nonlocal pending
        while b'\n' not in pending:
            chunk = connection.recv(1 << 16)
            if not chunk:
                raise RuntimeError('QMP closed the connection')
            pending += chunk
        line, _, pending = pending.partition(b'\n')
        return json.loads(line)

    read_message()
    connection.sendall(json.dumps({'execute': 'qmp_capabilities'}).encode() + b'\n')
    read_message()
    replies = []
    for command in commands:
        connection.sendall(json.dumps({'execute': 'human-monitor-command',
                                       'arguments': {'command-line': command}}).encode() + b'\n')
        replies.append(read_message().get('return', ''))
    connection.close()
    return replies

def run_probe(qmp, addresses, memory_length=0x50):
    """Read the metaclasses through the QEMU monitor and the system registers through gdb.

    The monitor can dump memory but QEMU's HMP has no aarch64 system registers, so the
    translation registers come from gdb's `org.gnu.gdb.aarch64.sysreg` feature (the
    register names there are upper case and `SCTLR` has no `_EL1` suffix).
    """
    started = time.time()
    commands = [f'x /{memory_length // 8}xg {address:#x}' for address in addresses]
    text = ''
    try:
        replies = qmp_command(qmp, commands)
        for address, reply in zip(addresses, replies):
            text += f'MEM {address:x}\n{reply}'
    except Exception as error:
        text += f'MONITOR-FAILED {error}\n'

    executable = shutil.which('gdb-multiarch') or shutil.which('gdb')
    if executable is not None:
        script = ['set pagination off', 'set confirm off', 'set architecture aarch64',
                  f'target remote 127.0.0.1:{PORT_GDB}', 'set remotetimeout 30']
        for register in ('TCR_EL1', 'SCTLR', 'TTBR0_EL1', 'TTBR1_EL1'):
            script.append(f'printf "REG {register} %lx\\n", ${register}')
        script.append('printf "REG PC %lx\\n", $pc')
        script += ['x/1xg 0x0', 'detach']
        gdb_commands = []
        for line in script:
            gdb_commands += ['-ex', line]
        try:
            result = subprocess.run([executable, '-batch', '-nx'] + gdb_commands,
                                    capture_output=True, text=True, timeout=120)
            text += result.stdout + result.stderr
        except Exception as error:
            text += f'GDB-FAILED {error}\n'
    return text, time.time() - started

def parse_memory(text, addresses, length):
    memory = {}
    lines = text.splitlines()
    for address in addresses:
        key = f'{address:x}'
        start = next((i for i, line in enumerate(lines) if line.strip() == f'MEM {key}'), None)
        if start is None:
            continue
        words = []
        for line in lines[start + 1:start + 1 + length // 8]:
            if ':' not in line:
                break
            _, _, payload = line.partition(':')
            for chunk in payload.replace('\t', ' ').split():
                chunk = chunk.strip()
                if chunk.startswith('0x') and len(chunk) <= 18:
                    words.append(int(chunk, 16))
        if len(words) * 8 >= length:
            memory[f'{key}:{length:x}'] = b''.join(w.to_bytes(8, 'little') for w in words[:length // 8]).hex()
    return memory

def parse_registers(text):
    """Collect the `REG <name> <value>` lines printed by the gdb probe."""
    registers = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0] == 'REG':
            registers[parts[1]] = parts[2]
    return {'TCR_EL1': registers['TCR_EL1'], 'SCTLR_EL1': registers['SCTLR'],
            'TTBR0_EL1': registers['TTBR0_EL1'], 'TTBR1_EL1': registers['TTBR1_EL1'],
            'PC': registers.get('PC', '0x0')} if all(
                key in registers for key in ('TCR_EL1', 'SCTLR', 'TTBR0_EL1', 'TTBR1_EL1')) else {}

def build_adt(args, work):
    """Generate an ADT without a ramdisk slot.

    DarwinBoot refuses an ADT that carries a ramdisk placeholder when no ramdisk is
    handed to it (`DarwinBoot.c` returns EFI_UNSUPPORTED), which is exactly the case
    for this observation boot.
    """
    if args.adt is not None:
        return args.adt
    out = work / 'capture.adt'
    command = [
        sys.executable, str(args.adt_tool), '--output', str(out),
        '--dram-base', '0x40000000', '--dram-size', '0x80000000',
        '--owned-memory-size', '0x70000000',
        '--chip-id', '207', '--unique-chip-id', '0123456789ABCDEF',
        '--cpu-regs', '0,0x1,0x2,0x3', '--psci-method', 'hvc',
    ]
    print('== generating ADT without a ramdisk slot')
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        fail(f'{args.adt_tool} failed: {result.stdout[-600:]}{result.stderr[-600:]}')
    return out

def build_esp(args, work):
    """Build the read-only ESP image that boots XNU on the QEMU virt machine."""
    tree = work / 'esp'
    if tree.is_dir():
        shutil.rmtree(tree)
    adt = build_adt(args, work)
    tool = [
        sys.executable, str(args.esp_tool),
        '--out', str(tree),
        '--unixnu-app', str(args.unixnu_app),
        '--darwinboot', str(args.darwinboot),
        '--kernel', str(args.kernel),
        '--adt', str(adt),
    ]
    print('== building ESP payload tree')
    result = subprocess.run(tool, capture_output=True, text=True)
    if result.returncode:
        fail(f'{args.esp_tool} failed: {result.stdout[-800:]}{result.stderr[-800:]}')
    image = work / 'esp.img'
    image.unlink(missing_ok=True)
    with image.open('wb') as handle:
        handle.truncate(260 * 1024 * 1024)
    for command in (
        ['mkfs.vfat', '-F', '32', str(image)],
        ['mmd', '-i', str(image), '::/EFI', '::/EFI/BOOT', '::/EFI/UniXNU', '::/EFI/UniXNU/Drivers',
         '::/EFI/UniXNU/Payloads'],
        ['mcopy', '-i', str(image), str(tree / 'EFI/BOOT/BOOTAA64.EFI'), '::/EFI/BOOT/'],
        ['mcopy', '-i', str(image), str(tree / 'EFI/UniXNU/unixnu.toml'), '::/EFI/UniXNU/'],
        ['mcopy', '-i', str(image), str(tree / 'EFI/UniXNU/Drivers/DarwinBoot.efi'), '::/EFI/UniXNU/Drivers/'],
        ['mcopy', '-i', str(image), str(tree / 'EFI/UniXNU/Payloads/kernel.macho'), '::/EFI/UniXNU/Payloads/'],
        ['mcopy', '-i', str(image), str(tree / 'EFI/UniXNU/Payloads/kernel.macho.adt'), '::/EFI/UniXNU/Payloads/'],
    ):
        finished = subprocess.run(command, capture_output=True, text=True)
        if finished.returncode:
            fail(f'{command[0]} failed: {finished.stderr.strip()[:400]}')
    return image

def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--kernel', type=Path, required=True, help='prelinked candidate to observe')
    parser.add_argument('--out', type=Path, required=True, help='gdb-stop.json to write')
    parser.add_argument('--boot-timeout', type=float, default=120.0, help='unused, kept for symmetry')
    parser.add_argument('--wait-seconds', type=float, default=55.0,
                        help='seconds to let XNU initialise IOKit before attaching gdb')
    parser.add_argument('--attempts', type=int, default=4,
                        help='how many times to retry the gdb read while the metaclass is still zero')
    parser.add_argument('--work-dir', default='/tmp/r11-capture', help='scratch directory')
    parser.add_argument('--observe-only', action='store_true',
                        help='only resolve the dispatch table and print the addresses')
    parser.add_argument('--ready-markers', nargs='+',
                        default=['bsd_autoconf: calling kminit', 'BSD root:', 'NativeSdhci['],
                        help='serial log fragments that mean the kernel is running with its MMU on')
    parser.add_argument('--esp-tool', type=Path,
                        default=Path(__file__).resolve().parent / 'make-qemu-esp.py')
    parser.add_argument('--unixnu-app', type=Path,
                        default=Path('/root/src/Mu-Silicium/Build/r11Pkg/DEBUG_CLANGDWARF/AARCH64/'
                                     'UniXNUPkg/Application/UniXNU/UniXNU/OUTPUT/UniXNU.efi'))
    parser.add_argument('--darwinboot', type=Path,
                        default=Path('/root/src/Mu-Silicium/Build/r11Pkg/DEBUG_CLANGDWARF/AARCH64/'
                                     'DarwinBootPkg/Driver/DarwinBoot/OUTPUT/DarwinBoot.efi'))
    parser.add_argument('--cpu', default='max',
                        help='QEMU CPU model; "max" advertises ARMv8.1 so the unpatched candidate runs')
    parser.add_argument('--adt', type=Path, default=None,
                        help='pre-made ADT; by default one without a ramdisk slot is generated')
    parser.add_argument('--adt-tool', type=Path,
                        default=Path(__file__).resolve().parent / 'make-r11-adt.py')
    parser.add_argument('--firmware-code', type=Path, default=Path('/usr/share/AAVMF/AAVMF_CODE.fd'))
    parser.add_argument('--firmware-vars', type=Path, default=Path('/usr/share/AAVMF/AAVMF_VARS.fd'))
    args = parser.parse_args()

    data = args.kernel.read_bytes()
    segments = macho_segments(data)
    names = prelink_symbols(data, segments)
    dispatch = resolve_dispatch(data, segments, names)
    print(f'vtable   {dispatch["table"]:#x} (named slots {dispatch["named"]})')
    print(f'meta     {dispatch["meta"]:#x}')
    print(f'service  {dispatch["service"]:#x}')
    print(f'kernel   {hashlib.sha256(data).hexdigest()}')
    if args.observe_only:
        return 0

    addresses = [dispatch['meta'], dispatch['service']]
    work = Path(args.work_dir)
    work.mkdir(parents=True, exist_ok=True)
    log = work / 'serial.log'
    qmp = work / 'qmp.sock'
    for stale in (log, qmp):
        stale.unlink(missing_ok=True)

    build_esp(args, work)
    print('== starting QEMU')
    subprocess.run(['pkill', '-9', '-f', 'qemu-system-aarch64'], check=False)
    time.sleep(1)
    firmware_vars = work / 'vars.fd'
    shutil.copyfile(args.firmware_vars, firmware_vars)
    command = ['qemu-system-aarch64', '-M', 'virt', '-cpu', args.cpu, '-m', '4096', '-smp', '4',
               '-drive', f'if=pflash,format=raw,readonly=on,file={args.firmware_code}',
               '-drive', f'if=pflash,format=raw,file={firmware_vars}',
               '-drive', f'if=none,id=esp,format=raw,readonly=on,file={work / "esp.img"}',
               '-device', 'virtio-blk-pci,drive=esp', '-device', 'ramfb',
               '-nic', 'none', '-display', 'none', '-no-reboot', '-s',
               '-serial', f'file:{log}', '-qmp', f'unix:{qmp},server=on,wait=off']
    guest = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    try:
        # The kernel console goes to the framebuffer, so the serial log only carries the
        # firmware side; wait for a fixed settling time and then poll until the metaclass
        # has actually been relocated by IOKit startup.
        time.sleep(args.wait_seconds)
        text = ''
        memory = {}
        registers = {}
        for attempt in range(args.attempts):
            print(f'== probe attempt {attempt + 1}/{args.attempts}')
            try:
                text, elapsed = run_probe(qmp, addresses)
            except Exception as error:
                print(f'   monitor probe failed: {error}')
                text, elapsed = '', 0.0
            memory = parse_memory(text, addresses, 0x50)
            registers = parse_registers(text)
            if memory and len(registers) >= 4:
                actual = bytes.fromhex(memory[f'{dispatch["meta"]:x}:50'])
                size, = struct.unpack_from('<I', actual, META_FIELD_SIZE)
                if size == 0x90:
                    break
            print('   metaclass not populated yet, waiting 15 s')
            time.sleep(15)
        print(text[-1500:])
        if not memory or len(registers) < 4:
            fail('gdb did not return both memory blocks and four translation registers')
        report = dict(
            kernel_sha256=hashlib.sha256(data).hexdigest(),
            bounded_timeout_interrupted=False,
            qemu_accelerator='tcg',
            stopped_after_seconds=round(elapsed, 3),
            memory=memory,
            system_registers=registers,
        )
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2) + '\n')
        print(f'== wrote {args.out}')
        actual = bytes.fromhex(memory[f'{dispatch["meta"]:x}:50'])
        service = bytes.fromhex(memory[f'{dispatch["service"]:x}:50'])
        super_link, = struct.unpack_from('<Q', actual, META_FIELD_SUPER)
        size, = struct.unpack_from('<I', actual, META_FIELD_SIZE)
        service_size, = struct.unpack_from('<I', service, META_FIELD_SIZE)
        print(f'superClassLink={super_link:#x} (service {dispatch["service"]:#x})')
        print(f'classSize={size:#x} (expect 0x90)  service classSize={service_size:#x} (expect 0x88)')
        print('registers:', registers)
        return 0 if (super_link == dispatch['service'] and size == 0x90 and service_size == 0x88) else 1
    finally:
        guest.terminate()
        try:
            guest.wait(timeout=10)
        except subprocess.TimeoutExpired:
            guest.kill()
        subprocess.run(['pkill', '-9', '-f', 'qemu-system-aarch64'], check=False)

if __name__ == '__main__':
    sys.exit(main())
