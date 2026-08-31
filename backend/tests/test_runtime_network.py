from __future__ import annotations

import pytest

from runtime_network import (
    NetworkSettingsValidationError,
    normalize_network_settings,
    parse_runtime_network_snapshot,
    runtime_network_snapshot,
)


def test_normalize_network_settings_canonicalizes_modes_and_public_origin() -> None:
    """Normalize the next-launch state before any launcher file write."""
    assert normalize_network_settings("local", 6201, "") == {
        "mode": "local",
        "host": "127.0.0.1",
        "port": 6201,
        "public_url": "",
    }
    assert normalize_network_settings("lan", 6202, "") == {
        "mode": "lan",
        "host": "0.0.0.0",
        "port": 6202,
        "public_url": "",
    }
    assert normalize_network_settings("public", 6203, "https://staff.example.com/") == {
        "mode": "public",
        "host": "0.0.0.0",
        "port": 6203,
        "public_url": "https://staff.example.com",
    }


@pytest.mark.parametrize(
    ("mode", "port", "public_url"),
    [
        ("local", 0, ""),
        ("lan", 65_536, ""),
        ("public", 6201, ""),
        ("public", 6201, "javascript:alert(1)"),
        ("public", 6201, "https://user:password@staff.example.com"),
        ("public", 6201, "https://staff.example.com/?access_token=secret"),
        ("public", 6201, "https://staff.example.com/#secret"),
    ],
)
def test_normalize_network_settings_rejects_unsafe_or_invalid_values(
    mode: str,
    port: int,
    public_url: str,
) -> None:
    """Reject malformed settings before they can replace the persisted configuration."""
    with pytest.raises(NetworkSettingsValidationError):
        normalize_network_settings(mode, port, public_url)


def test_runtime_network_snapshot_uses_launcher_selected_port_not_request_headers() -> None:
    """Represent the actual launch contract independently of HTTP request headers."""
    snapshot = runtime_network_snapshot(
        {"mode": "lan", "host": "0.0.0.0", "port": 6204, "public_url": ""}
    )

    assert parse_runtime_network_snapshot(snapshot) == {
        "mode": "lan",
        "host": "0.0.0.0",
        "port": 6204,
        "public_url": "",
        "local_origin": "http://127.0.0.1:6204",
    }
