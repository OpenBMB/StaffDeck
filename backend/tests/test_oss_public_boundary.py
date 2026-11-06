import importlib.util
from pathlib import Path

import pytest

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('source_release',ROOT/'packaging/source_release.py')
release=importlib.util.module_from_spec(spec);spec.loader.exec_module(release)


def test_public_product_has_no_internal_management_implementations():
    release.validate(ROOT,names=['LICENSE','CLI.md','distribution/manifest.json'])
    for name in ('frontend-business','business_plugger','extensions','deploy'):
        assert not (ROOT/name).exists()


@pytest.mark.parametrize('path',[
    'business_plugger/src/private.py','frontend-business/src/App.tsx',
    'packaging/build_worker.py','backend/data/private.db','backend/.env',
    'harness-engine/.dsh-home-test/session.json',
])
def test_source_release_rejects_private_or_live_paths(path):
    with pytest.raises(ValueError):release.validate(ROOT,names=[path])


def test_semantic_release_versions():
    assert release.version_value('v0.6.0')=='0.6.0'
    with pytest.raises(ValueError):release.version_value('$(id)')
