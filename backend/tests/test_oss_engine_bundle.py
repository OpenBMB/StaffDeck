import importlib.util
from pathlib import Path

import pytest

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('bundle_engine',ROOT/'packaging/bundle_engine.py')
bundle=importlib.util.module_from_spec(spec);spec.loader.exec_module(bundle)


def test_staged_runtime_keeps_package_links_relocatable(tmp_path):
    source=tmp_path/'source';(source/'apps/cli/lib').mkdir(parents=True)
    (source/'apps/cli/lib/bin.js').write_text('console.log("ok")')
    (source/'packages/dep').mkdir(parents=True);(source/'packages/dep/index.js').write_text('export default 1')
    (source/'node_modules').mkdir()
    try:(source/'node_modules/dep').symlink_to('../packages/dep',target_is_directory=True)
    except OSError:pytest.skip('Host cannot create test symlinks')
    target=tmp_path/'app/harness-engine'
    bundle.stage(source,target)
    source.rename(tmp_path/'source-moved')
    assert (target/'node_modules/dep/index.js').read_text()=='export default 1'
    assert (target/bundle.MARKER).is_file()


def test_staging_rejects_links_outside_the_product(tmp_path):
    source=tmp_path/'source';(source/'apps/cli/lib').mkdir(parents=True)
    (source/'apps/cli/lib/bin.js').write_text('')
    (tmp_path/'private').mkdir()
    try:(source/'escape').symlink_to(tmp_path/'private',target_is_directory=True)
    except OSError:pytest.skip('Host cannot create test symlinks')
    with pytest.raises(ValueError,match='outside'):bundle.stage(source,tmp_path/'app')


def test_windows_payload_can_move_before_it_is_installed(tmp_path):
    from staffdeck_harness.distribution.runtime_bundle import install_runtime
    source=tmp_path/'source';(source/'apps/cli/lib').mkdir(parents=True)
    (source/'apps/cli/lib/bin.js').write_text('runtime')
    (source/'packages/dep').mkdir(parents=True);(source/'packages/dep/index.js').write_text('dependency')
    (source/'node_modules').mkdir()
    try:(source/'node_modules/dep').symlink_to('../packages/dep',target_is_directory=True)
    except OSError:pytest.skip('Host cannot create symlinks')
    archive=tmp_path/'build/harness-engine.tar.gz'
    bundle.archive_runtime(source,archive)
    archive.parent.rename(tmp_path/'installed')
    cache=install_runtime(tmp_path/'installed/harness-engine.tar.gz',tmp_path/'user-data')
    source.rename(tmp_path/'old-source')
    assert (cache/'node_modules/dep/index.js').read_text()=='dependency'
    assert install_runtime(tmp_path/'installed/harness-engine.tar.gz',tmp_path/'user-data')==cache


def test_native_payload_omits_prebuilds_for_other_architectures(tmp_path,monkeypatch):
    import tarfile
    source=tmp_path/'source';(source/'apps/cli/lib').mkdir(parents=True)
    (source/'apps/cli/lib/bin.js').write_text('runtime')
    for arch in ('darwin-arm64','darwin-x64'):
        p=source/'packages/pty/prebuilds'/arch/'pty.node';p.parent.mkdir(parents=True);p.write_text(arch)
    monkeypatch.setattr(bundle.platform,'system',lambda:'Darwin')
    monkeypatch.setattr(bundle.platform,'machine',lambda:'arm64')
    archive=tmp_path/'runtime.tar.gz';bundle.archive_runtime(source,archive)
    with tarfile.open(archive) as payload:
        names=payload.getnames()
    assert any('darwin-arm64/pty.node' in name for name in names)
    assert not any('darwin-x64' in name for name in names)


def test_runtime_cache_accepts_a_symlinked_data_directory(tmp_path):
    from staffdeck_harness.distribution.runtime_bundle import install_runtime
    source=tmp_path/'source';(source/'apps/cli/lib').mkdir(parents=True)
    (source/'apps/cli/lib/bin.js').write_text('runtime')
    (source/'packages/dep').mkdir(parents=True);(source/'packages/dep/index.js').write_text('dependency')
    (source/'node_modules').mkdir();real=tmp_path/'real';real.mkdir()
    try:
        (source/'node_modules/dep').symlink_to('../packages/dep',target_is_directory=True)
        (tmp_path/'alias').symlink_to(real,target_is_directory=True)
    except OSError:pytest.skip('Host cannot create symlinks')
    archive=tmp_path/'runtime.tar.gz';bundle.archive_runtime(source,archive)
    cache=install_runtime(archive,tmp_path/'alias/data')
    assert cache.is_relative_to(real)
    assert (cache/'node_modules/dep/index.js').read_text()=='dependency'
