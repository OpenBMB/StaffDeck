from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from staffdeck_harness.modules.presets import load_presets, save_preset


def test_presets_survive_settings_recreation_and_keep_secrets_out(tmp_path):
    settings = SimpleNamespace(harness_runtime_config_path=str(tmp_path / 'assembly.json'), harness_presets_dir='')
    assembly = {'engine': 'harness_v3', 'selections': {'source.staff': 'source.staff.local'},
                'disabled_modules': ['channel.feishu'], 'enabled_modules': ['team.provider'],
                'security_profile': 'OSS_LOCAL', 'module_configs': {'example': {'limit': 3}},
                'base': {'decision_token': 'DO_NOT_STORE'}, 'extra_modules': ['secret.package:register']}
    row = save_preset(settings, name='My preset', description='', assembly=assembly, source='saved', by='admin')
    again = load_presets(SimpleNamespace(**vars(settings)))
    assert again == [row]
    assert row['assembly'] == {key: value for key, value in assembly.items() if key not in {'base', 'extra_modules'}}
    assert 'DO_NOT_STORE' not in (tmp_path / 'assembly.presets.sqlite3').read_bytes().decode(errors='ignore')
    assert not (tmp_path / 'assembly.json').exists(), 'saving a preset must not change pending assembly'
    with pytest.raises(FileExistsError):
        save_preset(settings, name='my PRESET', description='', assembly={}, source='saved', by='admin')


def test_concurrent_duplicate_names_have_one_winner(tmp_path):
    settings = SimpleNamespace(harness_runtime_config_path=str(tmp_path / 'assembly.json'), harness_presets_dir='')
    def create(_):
        try:
            save_preset(settings, name='same', description='', assembly={}, source='saved', by='admin')
            return True
        except FileExistsError:
            return False
    with ThreadPoolExecutor(max_workers=3) as pool:
        assert sum(pool.map(create, range(3))) == 1
    assert len(load_presets(settings)) == 1


def test_preset_rejects_inline_credentials_and_blank_name(tmp_path):
    settings = SimpleNamespace(harness_runtime_config_path=str(tmp_path / 'assembly.json'), harness_presets_dir='')
    for name, assembly in [(' ', {}), ('x', {'module_configs': {'m': {'api_key': 'secret'}}})]:
        with pytest.raises(ValueError):
            save_preset(settings, name=name, description='', assembly=assembly, source='saved', by='admin')
