"""Register a source installation's CLI in the user's normal terminal PATH."""
from __future__ import annotations

import argparse
import json
import os
import shlex
import stat
import sys
from pathlib import Path

MARKER = '# StaffDeck managed CLI launcher'


def _writable(directory):
    return directory.is_dir() and os.access(directory, os.W_OK)


def _bin_directory():
    if os.name == 'nt':
        return Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'StaffDeck/bin'
    path = [Path(item).expanduser() for item in os.get_exec_path() if item]
    candidates = [Path.home() / '.local/bin', Path('/opt/homebrew/bin'), Path('/usr/local/bin')]
    for directory in candidates:
        if directory in path and _writable(directory):
            return directory
    return Path.home() / '.local/bin'


def _add_shell_path(directory):
    """Used only when no existing standard writable PATH directory is available."""
    shell = Path(os.environ.get('SHELL', '')).name
    config = Path.home() / ('.zshrc' if shell == 'zsh' else '.profile')
    if config.exists() and (config.is_symlink() or not config.is_file()):
        raise ValueError('Shell configuration is not a regular file')
    content = config.read_text() if config.exists() else ''
    line = f'export PATH={shlex.quote(str(directory))}:"$PATH"'
    if line not in content:
        with config.open('a') as target:
            target.write('\n# StaffDeck CLI\n' + line + '\n')


def install(root, *, base_url=None, data_dir=None,
            config_dir=None, bin_dir=None, executable=None, native=False):
    root = Path(root).resolve()
    manifest = json.loads((root / 'distribution/manifest.json').read_text())
    tenant = manifest['installation']['tenant_id']
    python = Path(executable or sys.executable).absolute()
    directory = Path(bin_dir) if bin_dir else _bin_directory()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / ('staffdeck.cmd' if os.name == 'nt' else 'staffdeck')
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not stat.S_ISREG(target.lstat().st_mode):
            raise ValueError('Existing staffdeck command is not a managed launcher')
        if MARKER not in target.read_text():
            raise ValueError('An existing staffdeck command belongs to another installation')
    settings = {'STAFFDECK_INSTALL_ROOT': str(root), 'STAFFDECK_TENANT_ID': tenant}
    if base_url:
        settings['STAFFDECK_BASE_URL'] = base_url
    if data_dir:
        settings['ULTRARAG_DATA_DIR'] = str(Path(data_dir).resolve())
    if config_dir:
        settings['STAFFDECK_CONFIG_DIR'] = str(Path(config_dir).resolve())
    if os.name == 'nt':
        # Keep the wrapper outside the application directory containing the GUI exe.
        lines = ['@echo off', 'rem ' + MARKER]
        for key, value in settings.items():
            if any(char in value for char in '\r\n"%'):
                raise ValueError('Unsupported character in CLI installation path')
            lines.append(f'if not defined {key} set "{key}={value}"')
        module = '' if native else ' -m staffdeck_harness.cli'
        lines.append(f'"{python}"{module} %*')
        target.write_text('\r\n'.join(lines) + '\r\n')
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, 'Environment') as key:
            try:
                old, kind = winreg.QueryValueEx(key, 'Path')
            except FileNotFoundError:
                old, kind = '', winreg.REG_EXPAND_SZ
            if str(directory).casefold() not in {part.casefold() for part in old.split(';')}:
                winreg.SetValueEx(key, 'Path', 0, kind, str(directory) + ';' + old)
        # Notify Explorer so new terminals inherit the updated user PATH.
        import ctypes
        from ctypes import wintypes
        send = ctypes.windll.user32.SendMessageTimeoutW
        send.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPCWSTR,
                         wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
        result = ctypes.c_size_t()
        send(0xFFFF, 0x001A, 0, 'Environment', 0x0002, 5000, ctypes.byref(result))

    else:
        lines = ['#!/bin/sh', MARKER]
        for key, value in settings.items():
            lines.append(f'if [ -z "${{{key}:-}}" ]; then export {key}={shlex.quote(value)}; fi')
        module = '' if native else ' -m staffdeck_harness.cli'
        lines.append(f'exec {shlex.quote(str(python))}{module} "$@"')
        temporary = target.with_name('.staffdeck-install')
        # O_EXCL avoids following an existing file or link during publication.
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o755)
        try:
            with os.fdopen(fd, 'w') as output:
                output.write('\n'.join(lines) + '\n')
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        if not bin_dir and directory not in [Path(item) for item in os.get_exec_path()]:
            _add_shell_path(directory)
    return target


def install_native():
    """Register the console sibling when a desktop installation first opens."""
    if not getattr(sys, 'frozen', False):
        return
    executable = Path(sys.executable).with_name('staffdeck-cli.exe' if os.name == 'nt' else 'staffdeck-cli')
    root = Path(sys._MEIPASS)
    if executable.is_file() and (root / 'distribution/manifest.json').is_file():
        install(root, executable=executable, native=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--base-url')
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--config-dir', type=Path)
    parser.add_argument('--bin-dir', type=Path)
    args = parser.parse_args()
    command = install(args.root, base_url=args.base_url, data_dir=args.data_dir,
                      config_dir=args.config_dir, bin_dir=args.bin_dir)
    print(f'CLI 已安装：{command}\n现在可运行 staffdeck --help、staffdeck agent list、staffdeck chat。')


if __name__ == '__main__':
    main()
