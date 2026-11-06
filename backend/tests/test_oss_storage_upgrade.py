import base64
import hashlib
import json
import sqlite3

import pytest
from cryptography.fernet import Fernet

from staffdeck_harness.distribution.storage import LEGACY_APP_SECRET, resolve_storage


def legacy_db(path, secret):
    path.parent.mkdir(parents=True, exist_ok=True)
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    token = Fernet(key).encrypt(b'old-provider-key').decode()
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE model_configs (api_key_encrypted TEXT)')
        db.execute('INSERT INTO model_configs VALUES (?)', (token,))
    return token


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    for key in ('ULTRARAG_DATA_DIR', 'ULTRARAG_DOTENV', 'DATABASE_URL', 'APP_SECRET'):
        monkeypatch.delenv(key, raising=False)


def test_source_upgrade_reuses_env_database_and_original_key_with_private_backup(tmp_path):
    root, data = tmp_path / 'source', tmp_path / 'user-data'
    data.mkdir(); old = root / 'backend/skill_agent_loop.db'
    token = legacy_db(old, 'original-custom-key')
    (old.parent / '.env').write_text('DATABASE_URL=sqlite:///./skill_agent_loop.db\nAPP_SECRET=original-custom-key\n')
    result = resolve_storage(root, data, {'version': '0.6.0'})
    assert result['database_url'] == 'sqlite:///' + str(old)
    assert result['app_secret'] == 'original-custom-key'
    backups = list((data / 'backups').glob('*.db'))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute('SELECT api_key_encrypted FROM model_configs').fetchone()[0] == token
    if __import__('os').name != 'nt':
        assert backups[0].stat().st_mode & 0o777 == 0o600
    resolve_storage(root, data, {'version': '0.6.0'})
    assert list((data / 'backups').glob('*.db')) == backups


def test_explicit_isolated_install_never_adopts_another_source_database(tmp_path, monkeypatch):
    root, data = tmp_path / 'source', tmp_path / 'isolated-data'
    data.mkdir(); legacy_db(root / 'backend/skill_agent_loop.db', 'original-key')
    monkeypatch.setenv('ULTRARAG_DATA_DIR', str(data))
    result = resolve_storage(root, data, {})
    assert result['database_url'] == 'sqlite:///' + str(data / 'skill_agent_loop.db')
    assert not (data / 'backups').exists()


def test_upgrade_closes_database_handles_before_publishing_backup(tmp_path, monkeypatch):
    from pathlib import Path

    from staffdeck_harness.distribution import storage

    data = tmp_path / 'data'; data.mkdir()
    old = data / 'skill_agent_loop.db'
    token = legacy_db(old, LEGACY_APP_SECRET)
    connect, replace = sqlite3.connect, Path.replace
    opened = []

    def tracked_connect(*args, **kwargs):
        db = connect(*args, **kwargs)
        opened.append(db)
        return db

    def require_closed_handles(path, target):
        if path.suffix == '.tmp':
            assert len(opened) == 3  # credential check, source, backup
            for db in opened:
                with pytest.raises(sqlite3.ProgrammingError, match='closed'):
                    db.execute('SELECT 1')
        return replace(path, target)

    monkeypatch.setattr(storage.sqlite3, 'connect', tracked_connect)
    monkeypatch.setattr(Path, 'replace', require_closed_handles)
    resolve_storage(tmp_path, data, {'version': '0.6.0'}, frozen=True)
    backup = next((data / 'backups').glob('*.db'))
    from contextlib import closing
    with closing(connect(backup)) as db:
        assert db.execute('SELECT api_key_encrypted FROM model_configs').fetchone()[0] == token


def test_frozen_upgrade_preserves_legacy_default_key(tmp_path):
    data = tmp_path / 'user-data'; data.mkdir()
    legacy_db(data / 'skill_agent_loop.db', LEGACY_APP_SECRET)
    result = resolve_storage(tmp_path / 'bundle', data, {}, frozen=True)
    assert result['app_secret'] == LEGACY_APP_SECRET


def test_wrong_key_stops_before_backup_or_configuration_is_written(tmp_path, monkeypatch):
    data = tmp_path / 'data'; data.mkdir()
    legacy_db(data / 'skill_agent_loop.db', 'actual-old-key')
    monkeypatch.setenv('APP_SECRET', 'wrong-key')
    with pytest.raises(RuntimeError, match='APP_SECRET'):
        resolve_storage(tmp_path, data, {})
    assert not (data / 'backups').exists()
    assert not (data / 'installation-secrets.json').exists()


def test_two_database_candidates_require_an_explicit_choice(tmp_path):
    data = tmp_path / 'data'; data.mkdir()
    legacy_db(data / 'skill_agent_loop.db', LEGACY_APP_SECRET)
    legacy_db(tmp_path / 'backend/skill_agent_loop.db', LEGACY_APP_SECRET)
    with pytest.raises(RuntimeError, match='多个旧数据库'):
        resolve_storage(tmp_path, data, {})


def test_existing_distribution_key_survives_an_upgrade(tmp_path):
    data = tmp_path / 'data'; data.mkdir()
    legacy_db(data / 'skill_agent_loop.db', 'saved-original-key')
    (data / 'installation-secrets.json').write_text(json.dumps({'app_secret': 'saved-original-key'}))
    assert resolve_storage(tmp_path, data, {})['app_secret'] == 'saved-original-key'


def test_frozen_bootstrap_finds_node_beside_installed_binary(tmp_path, monkeypatch):
    import sys

    from staffdeck_harness.distribution import bootstrap
    data=tmp_path/'data';data.mkdir()
    root=tmp_path/'app';internal=root/'_internal';internal.mkdir(parents=True)
    node=root/'sandbox/bin/node';node.parent.mkdir(parents=True);node.write_text('node fixture')
    monkeypatch.setenv('ULTRARAG_DATA_DIR',str(data))
    monkeypatch.delenv('HARNESS_V3_NODE_BIN',raising=False)
    monkeypatch.setattr(sys,'frozen',True,raising=False)
    monkeypatch.setattr(sys,'executable',str(root/'staffdeck'))
    monkeypatch.setattr(bootstrap,'distribution_root',lambda:internal)
    monkeypatch.setattr(bootstrap,'load_manifest',lambda:{
        'assembly':{'security_profile':'OSS_LOCAL'},'installation':{},'version':'0.6.0'})
    bootstrap.configure_environment()
    assert __import__('os').environ['HARNESS_V3_NODE_BIN']==str(node)


def test_installed_wheel_uses_manifest_location_as_product_root(tmp_path, monkeypatch):
    from staffdeck_harness.distribution import bootstrap
    monkeypatch.setenv('STAFFDECK_DISTRIBUTION_MANIFEST',str(tmp_path/'app/distribution/manifest.json'))
    assert bootstrap.distribution_root()==tmp_path/'app'


def test_external_signing_key_is_not_duplicated_into_a_plaintext_cache(tmp_path,monkeypatch):
    from staffdeck_harness.distribution import bootstrap
    data=tmp_path/'data';data.mkdir()
    monkeypatch.setenv('ULTRARAG_DATA_DIR',str(data))
    monkeypatch.setenv('APP_SECRET','external-signing-key-for-this-fixture')
    monkeypatch.setenv('STAFFDECK_INITIAL_ADMIN_PASSWORD','initial-password-fixture')
    monkeypatch.setattr(bootstrap,'load_manifest',lambda:{
        'assembly':{'security_profile':'OSS_LOCAL'},'installation':{},'version':'0.6.0'})
    bootstrap.configure_environment()
    assert not (data/'installation-secrets.json').exists()
    assert not (data/'.installation-seed').exists()
    for path in data.rglob('*'):
        if path.is_file():
            assert b'external-signing-key-for-this-fixture' not in path.read_bytes()
            assert b'initial-password-fixture' not in path.read_bytes()


def test_empty_signing_environment_uses_persisted_random_material(tmp_path,monkeypatch):
    import os

    from staffdeck_harness.distribution import bootstrap
    data=tmp_path/'data';data.mkdir()
    monkeypatch.setenv('ULTRARAG_DATA_DIR',str(data))
    monkeypatch.setenv('APP_SECRET','')
    monkeypatch.setattr(bootstrap,'load_manifest',lambda:{
        'assembly':{'security_profile':'OSS_LOCAL'},'installation':{},'version':'0.6.0'})
    bootstrap.configure_environment()
    value=os.environ['APP_SECRET']
    assert len(value)==64 and len((data/'.installation-seed').read_bytes())==32
    bootstrap.configure_environment()
    assert os.environ['APP_SECRET']==value
