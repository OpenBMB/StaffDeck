from types import SimpleNamespace as NS
import os
import pytest

from staffdeck_harness.bridge.session_runner import _check_storage_error
from staffdeck_harness.contracts.errors import EngineStoragePermissionDenied


@pytest.mark.parametrize('operation', ['scandir', 'open', 'mkdir', 'stat', 'rename'])
def test_native_storage_error_has_safe_specific_code(operation):
    with pytest.raises(EngineStoragePermissionDenied) as exc:
        _check_storage_error(f"EACCES: permission denied, {operation} '/private/tenant/session'")
    assert exc.value.code == 'ENGINE_STORAGE_PERMISSION_DENIED'
    assert '/private/tenant' not in str(exc.value)


@pytest.mark.parametrize('message', ['API authentication failed', 'Model says EACCES: permission denied, scandir', 'Connection error.'])
def test_non_storage_errors_are_not_relabelled(message):
    _check_storage_error(message)


def test_wrong_runtime_user_cannot_write_existing_home(tmp_path, monkeypatch):
    from staffdeck_harness.bridge.worker import materialize_home
    home = tmp_path / 'engine-home'
    home.mkdir()
    monkeypatch.setattr(os, 'geteuid', lambda: home.stat().st_uid + 1)
    with pytest.raises(EngineStoragePermissionDenied):
        materialize_home(NS(harness_v3_home=home), mcp_url='http://localhost/mcp')
    assert list(home.iterdir()) == []
