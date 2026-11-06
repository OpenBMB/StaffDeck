import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(params=['prepare.py', 'packaging/distribution/prepare.py'])
def preparation(request):
    spec = importlib.util.spec_from_file_location('oss_prepare_test', ROOT / request.param)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_frontend_keeps_npm_lockfile(preparation, tmp_path):
    (tmp_path / 'package-lock.json').write_text('{}')
    (tmp_path / 'pnpm-lock.yaml').write_text('lockfileVersion: 6.0')
    assert preparation.package_manager(tmp_path) == ['npm']


def test_runtime_uses_pinned_pnpm_without_global_install(preparation, tmp_path):
    (tmp_path / 'pnpm-lock.yaml').write_text('lockfileVersion: 9.0')
    (tmp_path / 'package.json').write_text(json.dumps({'packageManager': 'pnpm@11.7.0'}))
    assert preparation.package_manager(tmp_path) == [
        'npm', 'exec', '--yes', '--package=pnpm@11.7.0', '--', 'pnpm',
    ]


def test_exported_preparation_matches_source_entrypoint():
    assert (ROOT / 'prepare.py').read_bytes() == (
        ROOT / 'packaging/distribution/prepare.py'
    ).read_bytes()
