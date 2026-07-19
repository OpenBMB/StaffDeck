import pytest

from app.channels.crypto import decrypt_channel_secret, encrypt_channel_secret
from app.config import get_settings


def test_encrypt_decrypt_roundtrip() -> None:
    token = encrypt_channel_secret("ilinkbot_token_123")
    assert token != "ilinkbot_token_123"
    assert decrypt_channel_secret(token) == "ilinkbot_token_123"


def test_key_derivation_stable_across_calls() -> None:
    first = encrypt_channel_secret("same-secret")
    second = encrypt_channel_secret("same-secret")
    # Fernet 带随机 IV,密文不同但密钥派生稳定,都能解回明文
    assert decrypt_channel_secret(first) == "same-secret"
    assert decrypt_channel_secret(second) == "same-secret"


def test_channel_secret_change_makes_old_token_undecryptable(monkeypatch) -> None:
    token = encrypt_channel_secret("same-secret")
    changed = get_settings().model_copy(update={"channel_secret": "another-secret"})
    monkeypatch.setattr("app.channels.crypto.get_settings", lambda: changed)
    with pytest.raises(Exception):
        decrypt_channel_secret(token)
