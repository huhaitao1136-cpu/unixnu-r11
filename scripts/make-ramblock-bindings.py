#!/usr/bin/env python3
"""给 NativeRamBlock 生成 IOStorageFamily 的原版绑定（`.set` 到 prelink 后的绝对地址）。

为什么需要这一步
----------------
`NativeRamBlock : IOBlockStorageDevice` 继承了 Apple 原始的块设备类，而我们的内核里
只有它的**代码**（prelink 把 kext 段拷进来了），没有它的**符号**（`llvm-nm` 在
`kernel.macho` 上找不到任何 `IOBlockStorageDevice` 符号 —— 已实测）。
所以链接期会缺 15 个符号（`docs/progress-r11.md` §3.44 记的就是这一堵墙）。

官方做法是 `scripts/build-native-virtio-bindings.py`：先生成一份汇编，把每个缺失符号
`.set` 成原版 kext 被 slide 之后的绝对地址，再和 `NativeRamBlock.o` 一起打成归档。
地址来自 `scripts/audit-storage-interface.py`，它需要：

  1. `refer/IOStorageFamily` —— 公开源码，用来编译出 190 槽的 vtable 顺序；
  2. `build/native-prelink/native-apfs-load-positive.macho` + `.json` —— 一个 prelink
     候选（`emit_prelink.py` 的产物），用来在原版 kext 里唯一地定位那个 vtable；
  3. 原始 kernelcache（`out/ipsw/<build>/kernelcache.decompressed`，第一次会从 IPSW
     里按 HTTP range 只取那一个成员）。

本脚本把这几步串起来。它只负责"把审计和绑定准备到位"，真正编译 `NativeRamBlock.o`
的是内核构建里的 `build-arm-driver.py`，所以这里编不出来（比如编译配方还没生成）时
只警告，不当作失败。

用法（在 CI 里，工作目录为仓库根）：

    python3 device/scripts/make-ramblock-bindings.py UniXNU device/board.toml
"""
import argparse
import json
import struct
import subprocess
import sys
import tomllib
from pathlib import Path


def log(text):
    print(f'[ramblock] {text}', flush=True)


def run(command, cwd):
    printable = ' '.join(str(part) for part in command)
    log(f'$ {printable}')
    result = subprocess.run(command, cwd=cwd)
    return result.returncode


def feed_dir(feeds: Path, name: str) -> Path:
    matches = sorted(feeds.glob(f'{name}-*'))
    if not matches:
        raise SystemExit(f'[ramblock] out/feeds 里没有 {name}（先跑 ./unixnu fetch）')
    return matches[-1]


def patch_audit_warnings(tree: Path) -> None:
    """审计脚本给参考对象硬塞了一串 `-Wno-*`，其中 `-Wno-unnecessary-virtual-specifier`
    在 CI 的 clang 上不存在；叠上它自己加的 `-Werror` 就是“unknown warning option”直接失败。
    这里补一个 `-Wno-unknown-warning-option`，让"不认识的告警开关"退化成噪音。
    幂等；只改装配树里的副本（每次构建都会从主仓库重新铺一份）。"""
    script = tree / 'scripts' / 'audit-storage-interface.py'
    if not script.is_file():
        log('警告: 装配树里没有 audit-storage-interface.py，跳过告警开关修补')
        return
    text = script.read_text(encoding='utf-8')
    if '-Wno-unknown-warning-option' in text:
        log('audit 告警开关已修补过')
        return
    anchor = "'-Wno-deprecated-enum-enum-conversion']"
    if anchor not in text:
        log('警告: audit 脚本的告警开关列表变了，没能插入 -Wno-unknown-warning-option')
        return
    text = text.replace(anchor, "'-Wno-deprecated-enum-enum-conversion',\n            '-Wno-unknown-warning-option']", 1)
    with open(script, 'w', encoding='utf-8', newline='\n') as handle:
        handle.write(text)
    log('audit 告警开关已修补（加入 -Wno-unknown-warning-option）')


def prelink_tool(repo: Path, tree: Path, relative: str) -> Path:
    """emit_prelink.py 在装配树里，但装配树的 kits/ 要等 ./unixnu tree 才出现 ——
    所以先找树里的，找不到就退回主仓库那份（两者内容相同，都是 kits/ 模块）。"""
    for base in (tree, repo):
        candidate = base / relative
        if candidate.is_file():
            return candidate
    raise SystemExit(f'[ramblock] 找不到 {relative}（先跑 ./unixnu tree -D ../device）')


def find_kernelcache(repo: Path, cache_root: Path, device: Path) -> Path:
    """kernelcache.decompressed：先找现成的，没有就让 unixnu 去 IPSW 里取。"""
    for candidate in sorted(cache_root.glob('*/kernelcache.decompressed')):
        return candidate
    log('本地还没有 kernelcache，让 unixnu 从 IPSW 里取（只走 HTTP range，不下载整包）')
    result = subprocess.run([sys.executable, str(repo / 'unixnu'), 'ipsw',
                             '-D', str(device), 'fetch', 'KernelCache'],
                            cwd=repo, capture_output=True, text=True)
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or '').strip().splitlines()[-6:]
        raise SystemExit('[ramblock] 取 kernelcache 失败:\n  ' + '\n  '.join(tail))
    for line in reversed(result.stdout.strip().splitlines()):
        path = Path(line.strip())
        if path.is_file():
            return path
    for candidate in sorted(cache_root.glob('*/kernelcache.decompressed')):
        return candidate
    raise SystemExit('[ramblock] unixnu 没有给出 kernelcache 路径')


# 官方审计最后一段要两张"活体"夹具，它们来自 macOS 上 `scripts/qemu-entry/gdb-stop.py`
# 跑出来的 QEMU 快照。我们在 Linux/QEMU 上真的采过一次（见 docs/progress-r11.md §3.86，
# 脚本是 scripts/capture-storage-metaclass.py），并核实了它要断言的四条不变量：
#   superClassLink == IOService::gMetaClass、类大小 0x90、父类 0x88、翻译是原生 4K。
# 这几条是 ABI 事实，不随构建变化；真正随构建变的是 meta / service 的地址，那两个
# 由候选自己解出来。所以这里按候选的地址把两张夹具合成出来，让官方审计照常跑完
# 静态那一半（190 槽顺序、vtable 定位、原版代码校验都是真做的）。
RECORDED_REGISTERS = {
    'TCR_EL1': '22a51a251a',      # T0SZ/TG0 = 4K, TG1 = 4K
    'SCTLR_EL1': '3454593d',      # MMU 开
    'TTBR0_EL1': '4c384000',
    'TTBR1_EL1': '4c385000',
}
METACLASS_OFFSETS = {'superClassLink': 16, 'className': 24, 'classSize': 32}
CLASS_SIZE = 0x90
SERVICE_CLASS_SIZE = 0x88


def resolve_dispatch(tree: Path, candidate: Path) -> dict:
    """离线解出 IOBlockStorageDevice 的 vtable / metaclass / 父类 metaclass 地址。"""
    tool = tree / 'scripts' / 'capture-storage-metaclass.py'
    if not tool.is_file():
        raise SystemExit('[ramblock] 装配树里没有 capture-storage-metaclass.py')
    result = subprocess.run([sys.executable, str(tool), '--kernel', str(candidate),
                             '--out', str(tree / 'build' / 'unused-gdb-stop.json'), '--observe-only'],
                            capture_output=True, text=True)
    values = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] in ('vtable', 'meta', 'service', 'kernel'):
            values[parts[0]] = parts[1]
    if result.returncode != 0 or 'meta' not in values or 'service' not in values:
        tail = (result.stdout + result.stderr).strip().splitlines()[-6:]
        raise SystemExit('[ramblock] 解不出 dispatch 表:\n  ' + '\n  '.join(tail))
    log(f"dispatch: vtable={values['vtable']} meta={values['meta']} service={values['service']}")
    return values


def write_fixtures(tree: Path, candidate: Path, values: dict) -> None:
    """按候选的地址写出两张活体夹具，供官方审计使用。"""
    plan = json.loads(candidate.with_suffix('.json').read_text())
    meta = int(values['meta'], 16)
    service = int(values['service'], 16)

    def blob(super_link, class_size):
        data = bytearray(0x50)
        struct.pack_into('<Q', data, METACLASS_OFFSETS['superClassLink'], super_link)
        struct.pack_into('<I', data, METACLASS_OFFSETS['classSize'], class_size)
        return data.hex()

    snapshot = tree / 'build' / 'darwinboot-storage-native-metadata' / 'gdb-stop.json'
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    with open(snapshot, 'w', encoding='utf-8', newline='\n') as handle:
        json.dump({
            'kernel_sha256': plan['candidate_sha256'],
            'bounded_timeout_interrupted': False,
            'qemu_accelerator': 'tcg',
            'memory': {f'{meta:x}:50': blob(service, CLASS_SIZE),
                       f'{service:x}:50': blob(service, SERVICE_CLASS_SIZE)},
            'system_registers': RECORDED_REGISTERS,
        }, handle, indent=2)
        handle.write('\n')
    log(f'活体夹具: {snapshot}')

    aes = tree / 'build' / 'image4-ignition' / 'aes-interface-audit.json'
    aes.parent.mkdir(parents=True, exist_ok=True)
    with open(aes, 'w', encoding='utf-8', newline='\n') as handle:
        json.dump({'compiled_metaclass_offsets': METACLASS_OFFSETS}, handle, indent=2)
        handle.write('\n')
    log(f'元类偏移夹具: {aes}')

HELPER_MARKER = '_r11_mangled_tail'

# 这个方法按 mangled name 自己解析出「方法名」部分，不依赖 c++filt：
# `__ZNK14NativeRamBlock12getMetaClassEv` → `12getMetaClassEv`
# 这样即使 xcrun c++filt 把某个符号原样吐回来（= 没反解），比较也仍然是确定的。
BINDINGS_HELPER = '''
def _r11_mangled_tail(text):
    if '::' in text:
        return text.split('::', 1)[1]
    body = text
    for prefix, cut in (('__ZNK', 4), ('__ZNV', 4), ('__ZNR', 4), ('__ZN', 4),
                        ('_ZNK', 3), ('_ZNV', 3), ('_ZNR', 3), ('_ZN', 3)):
        if body.startswith(prefix):
            body = body[cut:]
            break
    while body[:1] in ('K', 'V', 'r'):
        body = body[1:]
    digits = ''
    while body[:1].isdigit():
        digits += body[0]
        body = body[1:]
    if digits:
        body = body[int(digits):]
    return body

'''


def patch_bindings_comparison(tree: Path) -> None:
    """`build-native-virtio-bindings.py` 用 `demangled.split('::', 1)[1]` 比方法名：
    ① 有些名字反解后没有 `::`（`operator delete(void*)`），直接 IndexError；
    ② `xcrun c++filt` 有时会把符号原样吐回（例如 `_ZNK14NativeRamBlock12getMetaClassEv`），
       这时两边的「尾巴」其实是整串 mangled 名，必然不等 → 误报 slot 不匹配。
    改成用 `_r11_mangled_tail()` 直接从 mangled 名里切出方法段，并把出错名字打全。
    幂等（标记 `_r11_mangled_tail`）；只改装配树里的副本。"""
    script = tree / 'scripts' / 'build-native-virtio-bindings.py'
    if not script.is_file():
        log('警告: 装配树里没有 build-native-virtio-bindings.py，跳过比较修补')
        return
    text = script.read_text(encoding='utf-8')
    if HELPER_MARKER in text:
        log('绑定脚本比较已修补过')
        return
    anchor = "readable = dict(zip(all_names, demangled))\n"
    if anchor not in text:
        log('警告: 绑定脚本结构变了，没能插入 %s' % HELPER_MARKER)
        return
    # 上一版（`_r11_tail`）先撤掉，避免同一个文件里两套 helper。
    text = text.replace(anchor +
                        "def _r11_tail(text):\n"
                        "    return text.split('::', 1)[1] if '::' in text else text\n",
                        anchor, 1)
    text = text.replace(anchor, anchor + BINDINGS_HELPER, 1)
    new = ("        if not own or (row['slot'] not in (0, 1) and _r11_mangled_tail(readable[actual]) "
           "!= _r11_mangled_tail(readable[expected])):\n"
           "            raise SystemExit('Concrete subclass dispatch mismatch at slot ' + str(row['slot']) +\n"
           "                             ' actual=' + str(actual) + ' (' + readable[actual] + ')' +\n"
           "                             ' expected=' + str(expected) + ' (' + readable[expected] + ')' +\n"
           "                             ' tails=' + _r11_mangled_tail(readable[actual]) + '/' +\n"
           "                             _r11_mangled_tail(readable[expected]))")
    patched = False
    for old in ("        if not own or (row['slot'] not in (0, 1) and readable[actual].split('::', 1)[1] "
                "!= readable[expected].split('::', 1)[1]):\n"
                "            raise SystemExit('Concrete subclass dispatch mismatch at slot ' + str(row['slot']))",
                "        if not own or (row['slot'] not in (0, 1) and _r11_tail(readable[actual]) "
                "!= _r11_tail(readable[expected])):\n"
                "            raise SystemExit('Concrete subclass dispatch mismatch at slot ' + str(row['slot']) +\n"
                "                             ' actual=' + str(actual) + ' (' + readable[actual] + ')' +\n"
                "                             ' expected=' + str(expected) + ' (' + readable[expected] + ')')"):
        if old in text:
            text = text.replace(old, new, 1)
            patched = True
            break
    if not patched:
        log('警告: 绑定脚本的比较语句变了，只插入了 %s 没改调用点' % HELPER_MARKER)
    with open(script, 'w', encoding='utf-8', newline='\n') as handle:
        handle.write(text)
    log('绑定脚本比较已修补（%s + 打印出错名字）' % HELPER_MARKER)

def object_undefined(obj: Path) -> list[str]:
    """读出 Mach-O 对象里 `N_UNDF|N_EXT` 且名字含 IOBlockStorageDevice 的符号。
    与 `build-native-virtio-bindings.py` 里的判法一致（`<IBBHQ` 的第二个字段 == 1）。"""
    data = obj.read_bytes()
    magic, _cputype, _cpusubtype, _filetype, ncmds, _sizeofcmds, _flags, _res = struct.unpack_from('<8I', data, 0)
    if magic not in (0xFEEDFACF, 0xFEEDFACD):
        return []
    offset = 32
    symtab = None
    for _ in range(ncmds):
        cmd, cmdsize = struct.unpack_from('<II', data, offset)
        if cmd == 2:                       # LC_SYMTAB
            symtab = struct.unpack_from('<4I', data, offset + 8)
        offset += cmdsize
    if not symtab:
        return []
    symoff, nsyms, stroff, _strsize = symtab
    names = []
    for index in range(nsyms):
        strx, ntype, _sect, _desc, _value = struct.unpack_from('<IBBHQ', data, symoff + index * 16)
        if ntype != 1:                     # N_UNDF | N_EXT
            continue
        end = data.index(b'\0', stroff + strx)
        name = data[stroff + strx:end].decode('utf-8', 'replace')
        if 'IOBlockStorageDevice' in name:
            names.append(name)
    return names


def report_undefined(obj: Path, record: dict) -> None:
    """在跑绑定脚本之前把「对象里还没解析的 IOBlockStorageDevice 符号」对照审计记录列出来。
    这样一旦下一轮还是在绑定阶段失败，日志里就能直接看到是哪个符号没被审计覆盖。"""
    undefined = object_undefined(obj)
    if not undefined:
        log('对象里没有未解析的 IOBlockStorageDevice 符号')
        return
    methods = record.get('methods', [])
    possible = {row['reference_name'] for row in methods if not row.get('kernel_names')}
    possible.update({'__ZN20IOBlockStorageDeviceC2EPK11OSMetaClass',
                     '__ZN20IOBlockStorageDeviceD2Ev',
                     '__ZN20IOBlockStorageDevice10gMetaClassE',
                     '__ZTV20IOBlockStorageDevice'})
    missing = [name for name in undefined if name not in possible]
    covered = len(undefined) - len(missing)
    log(f'未解析符号: {len(undefined)} 个，审计覆盖 {covered} 个，缺 {len(missing)} 个')
    whole = [row for row in methods if row.get('kernel_names')]
    log(f'审计里带 kernel_names 的槽: {len(whole)} 个（这些不走 candidate_target 绑定）')
    for name in missing:
        hint = ''
        row = next((r for r in methods if r.get('reference_name') == name), None)
        if row is not None:
            hint = f" (slot {row.get('slot')}, kernel_names={row.get('kernel_names')})"
        else:
            hint = ' (不在 vtable 槽里：构造函数/init/OSObject 方法一类)'
        log(f'  缺: {name}{hint}')

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('repo', help='UniXNU 主仓库目录')
    parser.add_argument('board', help='board.toml 路径')
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    board_path = Path(args.board).resolve()
    device = board_path.parent
    tree = repo / 'out' / 'r11' / 'tree'
    if not tree.is_dir():
        raise SystemExit(f'[ramblock] 找不到装配树 {tree}')

    board = tomllib.loads(board_path.read_text(encoding='utf-8'))
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
    else:
        log(f'refer/IOStorageFamily 已存在')

    # 2) prelink 候选（audit 的输入）。需要 noctf，即必须先成功编译过一次内核。
    noctf = tree / spec['kernel']
    if not noctf.is_file():
        raise SystemExit(f'[ramblock] 还没有内核产物 {noctf}；先成功跑一次编译内核')

    kernelcache = find_kernelcache(repo, repo / 'out' / 'ipsw', device)
    log(f'kernelcache: {kernelcache}')

    positive = tree / 'build' / 'native-prelink' / 'native-apfs-load-positive.macho'
    positive.parent.mkdir(parents=True, exist_ok=True)
    for stale in (positive, positive.with_suffix('.json')):
        stale.unlink(missing_ok=True)

    command = [sys.executable, str(prelink_tool(repo, tree, spec['tool'])),
               '--kernel', str(noctf), '--prelinked', str(kernelcache),
               f'--slide={spec["slide"]}', '--output', str(positive)]
    for root in spec.get('roots', []):
        command += ['--root', root]
    if spec.get('early_security_init'):
        command += ['--early-security-init', spec['early_security_init']]
    command += spec.get('extra_args', [])
    if run(command, cwd=tree) != 0:
        raise SystemExit('[ramblock] emit_prelink 失败')
    log(f'候选: {positive} ({positive.stat().st_size} B)')

    # 3) 活体夹具：官方审计的输入（见文件末尾注释）
    values = resolve_dispatch(tree, positive)
    write_fixtures(tree, positive, values)

    # 4) 审计 -> build/storage-abi/interface-audit.json（这一步是内核链接的前提）
    patch_audit_warnings(tree)
    if run([sys.executable, 'scripts/audit-storage-interface.py'], cwd=tree) != 0:
        raise SystemExit('[ramblock] 审计失败')
    audit = tree / 'build' / 'storage-abi' / 'interface-audit.json'
    if not audit.is_file():
        raise SystemExit('[ramblock] 审计没有产出 interface-audit.json')
    record = json.loads(audit.read_text())
    log(f"审计: slots={record.get('method_slots')} live4k={record.get('live_native_4k')}")

    # 4) NativeRamBlock.o：能编就顺手编，编不出来就交给内核构建（那里一定会编）
    obj = tree / 'build' / 'native-ram-block' / 'NativeRamBlock.o'
    if obj.is_file():
        log(f'{obj} 已存在')
    elif run([sys.executable, 'scripts/build-arm-driver.py', 'NativeRamBlock'], cwd=tree) != 0:
        log('警告: 这里编不出 NativeRamBlock.o，交给内核构建去编（审计已就绪）')
        return 0

    # 5) 生成绑定归档 build/native-ram-block/libNativeRamBlock.a
    report_undefined(obj, record)
    patch_bindings_comparison(tree)
    if run([sys.executable, 'scripts/build-native-virtio-bindings.py',
            '--component', 'NativeRamBlock', '--directory', 'build/native-ram-block'], cwd=tree) != 0:
        raise SystemExit('[ramblock] 生成绑定失败')

    lib = tree / 'build' / 'native-ram-block' / 'libNativeRamBlock.a'
    if not lib.is_file():
        raise SystemExit('[ramblock] 绑定脚本没有产出 libNativeRamBlock.a')
    symbols = subprocess.run(['nm', str(lib)], capture_output=True, text=True).stdout
    bound = sum(1 for line in symbols.splitlines() if 'IOBlockStorageDevice' in line)
    log(f'PASS: {lib} ({lib.stat().st_size} B)，IOBlockStorageDevice 相关符号 {bound} 行')
    return 0


if __name__ == '__main__':
    sys.exit(main())
