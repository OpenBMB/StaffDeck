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


def install_runtime(archive: Path, data: Path) -> Path:
    # The application instance lock is held by the caller during startup.
    target=runtime_path(archive,data);expected=archive.with_suffix(archive.suffix+'.sha256').read_text(encoding='utf-8').strip()
    cache=target.parent;cache.mkdir(parents=True,exist_ok=True)
    if (target/'.complete').is_file() and (target/'apps/cli/lib/bin.js').is_file():return target
    if hashlib.sha256(archive.read_bytes()).hexdigest()!=expected:
        raise ValueError('Runtime checksum mismatch')
    with tempfile.TemporaryDirectory(prefix='install-',dir=cache) as temporary:
        output=(Path(temporary)/'runtime').resolve();output.mkdir();links=[]
        with tarfile.open(archive,'r:gz') as source:
            members=source.getmembers()
            if len(members)>250000 or sum(m.size for m in members)>5*1024**3:
                raise ValueError('Runtime archive exceeds installation limit')
            for member in members:
                relative=_relative(member.name)
                if member.issym():
                    resolved=(output/relative.parent/member.linkname).resolve()
                    if not resolved.is_relative_to(output):raise ValueError('Runtime link escapes payload: '+member.name)
                    links.append((relative,resolved.relative_to(output)))
                elif member.isfile() or member.isdir():
                    source.extract(member,output,filter='data')
                else:raise ValueError('Unsupported runtime archive entry')
        if not (output/'apps/cli/lib/bin.js').is_file():raise ValueError('Runtime CLI is missing')
        if target.exists():shutil.rmtree(target)
        output.replace(target)
        for relative,link in links:
            dest=target/relative;physical=target/link
            if not physical.exists():raise ValueError('Runtime link target is missing')
            if os.name=='nt' and physical.is_dir():
                import _winapi
                _winapi.CreateJunction(str(target/link),str(dest))
            elif os.name=='nt':os.link(physical,dest)
            else:dest.symlink_to(os.path.relpath(physical,dest.parent),target_is_directory=physical.is_dir())
        (target/'.complete').write_text(expected)
    return target


def ensure_runtime_archive():
    import sys
    if getattr(sys,'frozen',False) and os.name=='nt':
        archive=Path(sys.executable).resolve().parent/'harness-engine.tar.gz'
        if archive.is_file():
            from app.paths import user_data_dir
            install_runtime(archive,user_data_dir())
