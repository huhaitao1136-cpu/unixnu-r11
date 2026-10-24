#!/usr/bin/env python3
"""对 UniXNU 的 package 源码做 R11 需要的补丁（幂等）。

用法: patch-unixnu.py <tree/package 目录>

做的四件事：
 1. UniXNULog 直接写 ConOut（它原来的 DEBUG 走 DebugLib，可能被映射成空实现）
 2. 跳过 UniXNUConnectAll（连所有控制器会卡住）
 3. 跳过 gRT->SetWatchdogTimer（本固件实现可能挂死）
 4. 在 UniXNURun 各阶段插入分步标记，便于定位崩溃点

每次构建都会跑；重复执行不会叠加。
"""
import sys
from pathlib import Path

MAIN = "UniXNUPkg/Library/UniXNULib/Main.c"
APP = "UniXNUPkg/Application/UniXNU/UniXNU.c"

HELPER = '''
STATIC
VOID
R11Say (
  IN CONST CHAR8  *M
  )
{
  CHAR16  W[128];
  UINTN   J;

  for (J = 0; (M[J] != 0) && (J + 1 < sizeof (W) / sizeof (W[0])); J++) {
    W[J] = (CHAR16)(UINT8)M[J];
  }
  W[J] = 0;
  if ((gST != NULL) && (gST->ConOut != NULL)) {
    gST->ConOut->OutputString (gST->ConOut, W);
  }
}
'''

# 步骤标记：(锚点, 插入的位置说明)
STEPS = [
    ('  UniXNULogInit ();', '  R11Say ("UNIXNU: step: log init done\\r\\n");'),
    ('  UniXNUConfigLoad (ImageHandle, &Config);', '  R11Say ("UNIXNU: step: config loaded\\r\\n");'),
    ('  UniXNUPublishSlices (&Config);', '  R11Say ("UNIXNU: step: slices published\\r\\n");'),
    ('  UniXNUUiInit (&Config, &Ui);', '  R11Say ("UNIXNU: step: ui init done\\r\\n");'),
    ('    Status = UniXNUBootEntry (ImageHandle, &Config, &BootCopy);', '    R11Say ("UNIXNU: step: boot entry\\r\\n");'),
]

APP_STEP = ('  Host = UniXNUHostApplication;', '  R11Say ("UNIXNU: step: host selected\\r\\n");')

LOG_REPLACE = (
    '  DEBUG ((PcdGet32 (PcdUniXNUDebugLevel), "%a", Line));',
    '  DEBUG ((PcdGet32 (PcdUniXNUDebugLevel), "%a", Line));\n'
    '  R11Say (Line);'
)


def patch_main(path: Path) -> bool:
    s = path.read_text(encoding="utf-8", errors="replace")
    before = s

    # 1) 日志走 ConOut
    if LOG_REPLACE[1] not in s and LOG_REPLACE[0] in s:
        s = s.replace(LOG_REPLACE[0], LOG_REPLACE[1], 1)

    # 2) 跳过 ConnectAll
    s = s.replace('    UniXNUConnectAll ();',
                  '    /* R11: connect-all hangs, skipped */', 1)

    # 3) 跳过 SetWatchdogTimer
    s = s.replace('  gBS->SetWatchdogTimer (0, 0, 0, NULL);',
                  '  /* R11: gRT->SetWatchdogTimer may hang, skipped */', 1)

    # 4) 插入 helper（放在第一个函数定义之前）
    if 'R11Say (' not in s.split('UniXNULogInit')[0]:
        i = s.find('STATIC EFI_SERIAL_IO_PROTOCOL')
        if i < 0:
            i = s.find('VOID\nUniXNULogInit')
        if i > 0:
            s = s[:i] + HELPER + '\n' + s[i:]

    # 5) 分步标记
    for anchor, ins in STEPS:
        if ins.strip() not in s and anchor in s:
            s = s.replace(anchor, anchor + '\n' + ins, 1)

    # 旧的 entry reached 标记去掉（换成 R11Say 形式）
    s = s.replace('''  { const char *m = "UNIXNU: entry reached\\r\\n"; UINTN j;
    CHAR16 w[64];
    for (j = 0; m[j] && j < 63; j++) w[j] = (CHAR16)(UINT8)m[j];
    w[j] = 0;
    if (gST != NULL && gST->ConOut != NULL) { gST->ConOut->OutputString (gST->ConOut, w); } }
''', '  R11Say ("UNIXNU: entry reached\\r\\n");\n')

    if s != before:
        path.write_text(s, encoding="utf-8", newline="\n")
        return True
    return False


def patch_app(path: Path) -> bool:
    s = path.read_text(encoding="utf-8", errors="replace")
    before = s
    marker = '''  { const char *m = "UNIXNU: app entry\\r\\n"; UINTN j; CHAR16 w[64];
    for (j = 0; m[j] && j < 63; j++) { w[j] = (CHAR16)(UINT8)m[j]; }
    w[j] = 0; if (gST != NULL && gST->ConOut != NULL) { gST->ConOut->OutputString (gST->ConOut, w); } }
'''
    if 'UNIXNU: app entry' not in s:
        key = '  EFI_LOADED_IMAGE_PROTOCOL  *Loaded;'
        if key in s:
            s = s.replace(key, marker + key, 1)
    if s != before:
        path.write_text(s, encoding="utf-8", newline="\n")
        return True
    return False



CFG = "UniXNUPkg/Library/UniXNULib/Config.c"

CFG_STEPS = [
    ('  Status = UniXNUReadFile (Volume, (CONST CHAR16 *)PcdGetPtr (PcdUniXNUConfigPath), (VOID **)&Text, &Size);',
     '  R11Say ("UNIXNU: cfg: reading file\\r\\n");', 'after'),
    ('  Config->Volume = Volume;',
     '  R11Say ("UNIXNU: cfg: file read ok\\r\\n");', 'before'),
    ('  UniXNULog ("config: loaded %s", (CONST CHAR16 *)PcdGetPtr (PcdUniXNUConfigPath));',
     '  R11Say ("UNIXNU: cfg: toml parsed\\r\\n");', 'before'),
]


def patch_config(path: Path) -> bool:
    return False   # 探针已废弃（R11Say 是 Main.c 的 static，跨文件不可见）
    s = path.read_text(encoding="utf-8", errors="replace")
    before = s
    for anchor, ins, where in CFG_STEPS:
        if ins.strip() in s or anchor not in s:
            continue
        if where == "after":
            s = s.replace(anchor, anchor + "\n" + ins, 1)
        else:
            s = s.replace(anchor, ins + "\n" + anchor, 1)
    if s != before:
        path.write_text(s, encoding="utf-8", newline="\n")
        return True
    return False



VOL = "UniXNUPkg/Library/UniXNULib/Volume.c"

VOL_STEPS = [
    ('  Status = OpenPath (Volume, Path, &File);', '  R11Say ("UNIXNU: vol: open path\\r\\n");', 'before'),
    ('  Info = GetInfo (File);', '  R11Say ("UNIXNU: vol: opened, getting info\\r\\n");', 'before'),
    ('  ReadSize = (UINTN)Info->FileSize;', '  R11Say ("UNIXNU: vol: info ok, reading\\r\\n");', 'before'),
]


def patch_volume(path: Path) -> bool:
    return False   # 探针已废弃
    s2 = path.read_text(encoding="utf-8", errors="replace")
    before = s2
    for anchor, ins, where in VOL_STEPS:
        if ins.strip() in s2 or anchor not in s2:
            continue
        if where == "after":
            s2 = s2.replace(anchor, anchor + "\n" + ins, 1)
        else:
            s2 = s2.replace(anchor, ins + "\n" + anchor, 1)
    if s2 != before:
        path.write_text(s2, encoding="utf-8", newline="\n")
        return True
    return False



CFG_PATH_CONST = 'STATIC CONST CHAR16  mR11ConfigPath[] = L"\\\\EFI\\\\UniXNU\\\\unixnu.toml";\n'


def patch_config_path(path: Path) -> bool:
    """QEMU/AAVMF 里 UniXNU 自己的 PCD 不存在，PcdGetPtr 会返回无效指针。
    硬件固件里 PCD 完整，所以同一份二进制在硬件正常、在 QEMU 崩。
    这里把配置路径硬编码。"""
    s2 = path.read_text(encoding="utf-8", errors="replace")
    before = s2
    need = "(CONST CHAR16 *)PcdGetPtr (PcdUniXNUConfigPath)"
    if need in s2:
        if "mR11ConfigPath" not in s2:
            anchor = "#define UNIXNU_KIT_MANIFEST  L\"Kit.toml\"\n"
            if anchor in s2:
                s2 = s2.replace(anchor, anchor + "\n" + CFG_PATH_CONST, 1)
        s2 = s2.replace(need, "mR11ConfigPath")
    if s2 != before:
        path.write_text(s2, encoding="utf-8", newline="\n")
        return True
    return False



CFG_VOL = ["UniXNUPkg/Library/UniXNULib/Config.c",
           "UniXNUPkg/Library/UniXNULib/Volume.c"]


def strip_probes(path: Path) -> bool:
    """去掉 Config.c / Volume.c 里的 R11Say 探针（R11Say 是 Main.c 的 static，
    跨文件不可见，会编译失败）。"""
    s2 = path.read_text(encoding="utf-8", errors="replace")
    before = s2
    out = []
    for line in s2.splitlines(keepends=True):
        if 'R11Say ("UNIXNU: cfg:' in line or 'R11Say ("UNIXNU: vol:' in line:
            continue
        out.append(line)
    s2 = "".join(out)
    if s2 != before:
        path.write_text(s2, encoding="utf-8", newline="\n")
        return True
    return False


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    root = Path(sys.argv[1])
    main_c = root / MAIN
    app_c = root / APP
    if main_c.is_file():
        print("  patch-unixnu: Main.c", "updated" if patch_main(main_c) else "unchanged")
    else:
        print(f"  !! 缺少 {main_c}")
    for rel in CFG_VOL:
        f = root / rel
        if f.is_file():
            print(f"  patch-unixnu: {Path(rel).name} probes", "stripped" if strip_probes(f) else "clean")
    cfg_c0 = root / CFG
    if cfg_c0.is_file():
        print("  patch-unixnu: Config.c(path)", "updated" if patch_config_path(cfg_c0) else "unchanged")
    vol_c = root / VOL
    if vol_c.is_file():
        print("  patch-unixnu: Volume.c", "updated" if patch_volume(vol_c) else "unchanged")
    cfg_c = root / CFG
    if cfg_c.is_file():
        print("  patch-unixnu: Config.c", "updated" if patch_config(cfg_c) else "unchanged")
    if app_c.is_file():
        print("  patch-unixnu: UniXNU.c", "updated" if patch_app(app_c) else "unchanged")
    return 0


if __name__ == '__main__':
    sys.exit(main())
