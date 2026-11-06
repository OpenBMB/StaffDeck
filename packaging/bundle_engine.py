"""Stage a relocatable built Node runtime without PyInstaller's binary relocation."""
from __future__ import annotations

import argparse
import hashlib
import os
import platform
import posixpath
import shutil
import stat
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MARKER = '.staffdeck-harness-bundle'


def _other_prebuild(path: Path) -> bool:
    if path.parent.name != 'prebuilds' or not path.name.startswith(('darwin-','linux-','win32-','android-','freebsd-')):return False
    system={'windows':'win32','darwin':'darwin','linux':'linux'}.get(platform.system().lower())
    arch={'amd64':'x64','x86_64':'x64','aarch64':'arm64','arm64':'arm64'}.get(platform.machine().lower())
    return bool(system and arch and path.name != f'{system}-{arch}')


def archive_runtime(source: Path, destination: Path):
    source=source.resolve();destination.parent.mkdir(parents=True,exist_ok=True)
    with tarfile.open(destination,'w:gz',dereference=False) as output:
        def add(directory):
            for item in os.scandir(directory):
                src=Path(item.path);relative=src.relative_to(source)
                if item.name in {'.git','.agents','.cache','__pycache__','.dsh-home-test'} or item.name=='.env':continue
                if _other_prebuild(src):continue
                info=src.lstat()
                linked=src.is_symlink() or bool(getattr(info,'st_file_attributes',0)&getattr(stat,'FILE_ATTRIBUTE_REPARSE_POINT',0))
                entry=tarfile.TarInfo(relative.as_posix());entry.mode=stat.S_IMODE(info.st_mode)
                if linked:
                    target=src.resolve(strict=True)
                    if not target.is_relative_to(source):raise ValueError('Runtime link escapes source')
                    entry.type=tarfile.SYMTYPE
                    entry.linkname=posixpath.relpath(target.relative_to(source).as_posix(),relative.parent.as_posix())
                    output.addfile(entry)
                elif item.is_dir(follow_symlinks=False):
                    entry.type=tarfile.DIRTYPE;output.addfile(entry);add(src)
                else:
                    entry.size=info.st_size
                    with src.open('rb') as stream:output.addfile(entry,stream)
        add(source)
    destination.with_suffix(destination.suffix+'.sha256').write_text(hashlib.sha256(destination.read_bytes()).hexdigest())


def stage(source: Path, destination: Path):
    source, destination = source.resolve(), destination.absolute()
    if destination.is_relative_to(source):
        raise ValueError('Runtime destination cannot be inside its source')
    if not (source/'apps/cli/lib/bin.js').is_file():
        raise ValueError('Build Harness before staging the runtime')
    if os.name == 'nt':
        archive_runtime(source,destination.with_suffix('.tar.gz'))
        return
    if destination.exists():
        if not (destination/MARKER).is_file():
            raise FileExistsError('Refusing to replace an unmanaged runtime directory')
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='staffdeck-engine-',dir=destination.parent) as temporary:
        output=Path(temporary)/'runtime';output.mkdir()
        links=[]
        def copy(directory):
            for entry in os.scandir(directory):
                src=Path(entry.path);relative=src.relative_to(source);dest=output/relative
                if entry.name in {'.git','.agents','.cache','__pycache__','.dsh-home-test'} or entry.name=='.env':
                    continue
                if _other_prebuild(src):continue
                info=src.lstat()
                linked=src.is_symlink() or bool(getattr(info,'st_file_attributes',0) & getattr(stat,'FILE_ATTRIBUTE_REPARSE_POINT',0))
                if linked:
                    target=src.resolve(strict=True)
                    if not target.is_relative_to(source):
                        raise ValueError('Runtime link points outside the product: '+str(relative))
                    links.append((relative,target.relative_to(source),target.is_dir()))
                elif entry.is_dir(follow_symlinks=False):
                    dest.mkdir();copy(src)
                else:
                    shutil.copy2(src,dest)
        copy(source)
        for relative,target,is_dir in links:
            dest=output/relative
            if os.name=='nt' and is_dir:
                import _winapi
                _winapi.CreateJunction(str(destination/target),str(dest))
            elif os.name=='nt':
                os.link(output/target,dest)
            else:
                dest.symlink_to(os.path.relpath(output/target,dest.parent),target_is_directory=is_dir)
        (output/MARKER).write_text('StaffDeck OSS runtime\n')
        output.replace(destination)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination',type=Path)
    args=parser.parse_args()
    stage(ROOT/'harness-engine',args.destination)
