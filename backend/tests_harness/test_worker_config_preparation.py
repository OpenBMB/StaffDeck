import pytest

from staffdeck_harness.bridge.worker import HarnessV3WorkerConfig
from staffdeck_harness.contracts.errors import EngineUnavailable


def test_unprepared_oss_source_points_to_full_preparation(tmp_path):
    root = tmp_path / 'StaffDeck source'
    (root / 'distribution').mkdir(parents=True)
    (root / 'distribution/manifest.json').write_text('{}')
    (root / 'prepare.py').write_text('# source preparation')
    config = HarnessV3WorkerConfig(root / 'harness-engine', root / 'unused-home')

    with pytest.raises(EngineUnavailable) as error:
        config.validate()

    message = str(error.value)
    assert str(config.bin_js) in message
    assert 'python3 prepare.py --no-cli-link' in message
    assert str(root) in message
    assert 'Python SDK' in message
    assert 'run pnpm build:lib' not in message
    assert not config.harness_v3_home.exists()


@pytest.mark.parametrize('oss_parent', [False, True])
def test_external_engine_keeps_its_own_build_instructions(tmp_path, oss_parent):
    if oss_parent:
        (tmp_path / 'distribution').mkdir()
        (tmp_path / 'distribution/manifest.json').write_text('{}')
        (tmp_path / 'prepare.py').write_text('# prepares the bundled engine only')
    config = HarnessV3WorkerConfig(tmp_path / 'external-engine', tmp_path / 'unused-home')

    with pytest.raises(EngineUnavailable, match='run pnpm build:lib'):
        config.validate()

    assert not config.harness_v3_home.exists()


def test_prepared_source_still_checks_sdk_and_node(tmp_path, monkeypatch):
    config = HarnessV3WorkerConfig(tmp_path / 'harness-engine', tmp_path / 'unused-home')
    config.bin_js.parent.mkdir(parents=True)
    config.bin_js.write_text('// built entry point')
    with pytest.raises(EngineUnavailable, match='SDK not found'):
        config.validate()
    config.sdk_src.mkdir(parents=True)
    monkeypatch.setattr('staffdeck_harness.bridge.worker.shutil.which', lambda _name: None)
    with pytest.raises(EngineUnavailable, match='node binary'):
        config.validate()
    monkeypatch.setattr('staffdeck_harness.bridge.worker.shutil.which', lambda _name: '/bin/node')
    config.validate()
    assert not config.harness_v3_home.exists()
