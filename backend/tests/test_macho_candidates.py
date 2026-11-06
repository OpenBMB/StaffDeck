import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'packaging/macho_candidates.py'
spec = importlib.util.spec_from_file_location('macho_candidates', SCRIPT)
macho = importlib.util.module_from_spec(spec)
spec.loader.exec_module(macho)


@pytest.mark.parametrize('header', [
    'feedface', 'cefaedfe', 'feedfacf', 'cffaedfe',
    'cafebabe', 'bebafeca', 'cafebabf', 'bfbafeca',
])
def test_all_thin_and_universal_header_byte_orders_are_retained(tmp_path, header):
    binary = tmp_path / 'native'
    binary.write_bytes(bytes.fromhex(header) + b'\0' * 32)
    assert macho.candidates(tmp_path) == [binary]


def test_resources_and_links_are_skipped_but_nested_code_is_retained(tmp_path):
    (tmp_path / 'script.js').write_text('console.log("Mach-O");')
    (tmp_path / 'empty').touch()
    nested = tmp_path / 'Resources/Frameworks/native'
    nested.parent.mkdir(parents=True)
    nested.write_bytes(bytes.fromhex('cffaedfe') + b'\0' * 32)
    try:
        (tmp_path / 'alias').symlink_to(nested)
        (tmp_path / 'directory-alias').symlink_to(nested.parent, target_is_directory=True)
    except OSError:
        pytest.skip('Host cannot create fixture symlinks')
    assert macho.candidates(tmp_path) == [nested]


@pytest.mark.skipif(os.name == 'nt', reason='POSIX file-name fixture')
def test_null_output_keeps_newlines_in_paths_and_signs_deepest_first(tmp_path):
    parent = tmp_path / 'top\nlevel'
    child = tmp_path / 'nested/binary'
    child.parent.mkdir()
    for path in (parent, child):
        path.write_bytes(bytes.fromhex('feedfacf'))
    output = subprocess.check_output([sys.executable, SCRIPT, tmp_path])
    assert output.split(b'\0') == [bytes(child), bytes(parent), b'']


@pytest.mark.skipif(not hasattr(os, 'mkfifo'), reason='POSIX fixture')
def test_nonregular_files_cannot_block_the_header_scan(tmp_path):
    os.mkfifo(tmp_path / 'pipe')
    assert subprocess.check_output([sys.executable, SCRIPT, tmp_path], timeout=3) == b''
