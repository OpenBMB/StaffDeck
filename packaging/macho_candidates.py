"""List possible Mach-O files without starting a process for each resource.

This is only a fast prefilter: build_macos.sh still asks file(1) and lipo(1)
to validate every candidate. Java class files share the fat-binary magic and
are deliberately left for file(1) to distinguish.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Thin 32/64-bit and universal 32/64-bit headers, in both byte orders.
MAGIC = frozenset(bytes.fromhex(value) for value in (
    'feedface', 'cefaedfe', 'feedfacf', 'cffaedfe',
    'cafebabe', 'bebafeca', 'cafebabf', 'bfbafeca',
))


def candidates(root: Path) -> list[Path]:
    result = []
    for directory, _, files in os.walk(root, followlinks=False):
        for name in files:
            path = Path(directory) / name
            if path.is_symlink() or not path.is_file():
                continue
            try:
                with path.open('rb') as source:
                    header = source.read(4)
            except OSError:
                continue
            if header in MAGIC:
                result.append(path)
    return sorted(result, key=lambda path: len(path.parts), reverse=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    args = parser.parse_args()
    for item in candidates(args.root):
        sys.stdout.buffer.write(os.fsencode(item) + b'\0')
