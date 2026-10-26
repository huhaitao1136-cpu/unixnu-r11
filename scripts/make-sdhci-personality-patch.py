"""Generate the XNU personality patch for NativeSdhci from the assembled work copy.

Usage (the source copy lives outside the device repository):
    python unixnu-r11/scripts/make-sdhci-personality-patch.py ../out/kct.cpp

It reads the post-native-aes KernelConfigTables.cpp, inserts the
XNU_NATIVE_SDHCI block right before the closing ")"; and writes a git patch to
unixnu-r11/patches/xnu/native-sdhci-personality.patch.  Re-running is safe: an
already-inserted block makes it exit without touching the patch.
"""

import difflib
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT.parent / 'out' / 'kct.cpp'
PATCH = ROOT / 'scripts' / 'data' / 'native-sdhci-personality.patch'

BLOCK = '''#if defined(XNU_NATIVE_SDHCI) && XNU_NATIVE_SDHCI
    "   ,{"
    "     'IOClass'         = NativeSdhci;"
    "     'IOProviderClass' = IOPlatformDevice;"
    "     'IOProbeScore'    = 1000:32;"
    "   }"
#endif
'''

MARKER = 'XNU_NATIVE_SDHCI'
ANCHOR = '    ")";\n'


def main():
    text = SOURCE.read_text()
    if MARKER in text:
        print('source already carries the XNU_NATIVE_SDHCI block; nothing to do')
        return 0
    if text.count(ANCHOR) != 1:
        print(f'anchor {ANCHOR!r} appears {text.count(ANCHOR)} times, expected 1')
        return 1
    patched = text.replace(ANCHOR, BLOCK + ANCHOR)
    diff = difflib.unified_diff(
        text.splitlines(keepends=True),
        patched.splitlines(keepends=True),
        fromfile='a/iokit/KernelConfigTables.cpp',
        tofile='b/iokit/KernelConfigTables.cpp',
        n=3,
    )
    body = ''.join(diff)
    if not body:
        print('empty diff')
        return 1
    header = 'diff --git a/iokit/KernelConfigTables.cpp b/iokit/KernelConfigTables.cpp\n'
    PATCH.parent.mkdir(parents=True, exist_ok=True)
    # newline='\n' keeps the patch LF-only: a CRLF patch never matches the work tree.
    with PATCH.open('w', encoding='utf-8', newline='\n') as handle:
        handle.write(header + body)
    print(f'wrote {PATCH} ({len(header + body)} B)')
    print(body)
    return 0


if __name__ == '__main__':
    sys.exit(main())
