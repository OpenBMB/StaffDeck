"""Install the Windows Node payload into a versioned user cache with local links."""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import tarfile
import tempfile
from pathlib import Path, PurePosixPath


def _relative(value):
    path=PurePosixPath(value)
    if path.is_absolute() or '..' in path.parts or re.match(r'^[A-Za-z]:',value) or '\\' in value:
        raise ValueError('Unsafe runtime archive member')
    return Path(*path.parts)


def runtime_path(archive: Path, data: Path) -> Path:
    archive, data = archive.resolve(), data.resolve()
    expected=archive.with_suffix(archive.suffix+'.sha256').read_text(encoding='utf-8').strip()
    if not re.fullmatch(r'[a-f0-9]{64}',expected):raise ValueError('Invalid runtime checksum')
    return data/'engine-runtime'/expected[:24]


def _runtime_io_path(path: Path) -> Path:
    if os.name != "nt":
        return path
    value = str(path)
    if value.startswith("\\\\?\\"):
        return path
    if value.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + value[2:])
    return Path("\\\\?\\" + value)


def install_runtime(archive: Path, data: Path) -> Path:
    # The application instance lock is held by the caller during startup.
    target=runtime_path(archive,data);expected=archive.with_suffix(archive.suffix+'.sha256').read_text(encoding='utf-8').strip()
    target_io = _runtime_io_path(target)
    cache = target_io.parent
    cache.mkdir(parents=True, exist_ok=True)
    if (target_io/'.complete').is_file() and (target_io/'apps/cli/lib/bin.js').is_file():return target
    if hashlib.sha256(archive.read_bytes()).hexdigest()!=expected:
        raise ValueError('Runtime checksum mismatch')
    with tempfile.TemporaryDirectory(prefix='install-',dir=cache) as temporary:
        output = _runtime_io_path(Path(temporary).resolve() / 'runtime')
        output.mkdir()
        links = []
        with tarfile.open(archive,'r:gz') as source:
            members=source.getmembers()
            if len(members)>250000 or sum(m.size for m in members)>5*1024**3:
                raise ValueError('Runtime archive exceeds installation limit')
            for member in members:
                relative=_relative(member.name)
                if member.issym():
                    resolved = _runtime_io_path((output / relative.parent / member.linkname).resolve())
                    if not resolved.is_relative_to(output):raise ValueError('Runtime link escapes payload: '+member.name)
                    links.append((relative,resolved.relative_to(output)))
                elif member.isfile() or member.isdir():
                    source.extract(member,output,filter='data')
                else:raise ValueError('Unsupported runtime archive entry')
        if not (output/'apps/cli/lib/bin.js').is_file():raise ValueError('Runtime CLI is missing')
        if target_io.exists():shutil.rmtree(target_io)
        output.replace(target_io)
        for relative,link in links:
            dest = target_io / relative
            physical = target_io / link
            if not physical.exists():raise ValueError('Runtime link target is missing')
            if os.name=='nt' and physical.is_dir():
                import _winapi
                # CreateJunction adds its own NT prefix to the junction target.
                _winapi.CreateJunction(str(target / link), str(dest))
            elif os.name=='nt':os.link(physical,dest)
            else:dest.symlink_to(os.path.relpath(physical,dest.parent),target_is_directory=physical.is_dir())
        (target_io/'.complete').write_text(expected)
    return target


def ensure_runtime_archive():
    import sys
    if getattr(sys,'frozen',False) and os.name=='nt':
        archive=Path(sys.executable).resolve().parent/'harness-engine.tar.gz'
        if archive.is_file():
            from app.paths import user_data_dir
            install_runtime(archive,user_data_dir())
