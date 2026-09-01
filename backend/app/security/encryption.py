import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


def _fernet() -> Fernet:
    secret = get_settings().app_secret.encode("utf-8")
    key = base64.urlsafe_b64encode(hashlib.sha256(secret).digest())
    return Fernet(key)


def encrypt_secret(value: str) -> str:
    return _fernet().encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_secret(value: str) -> str:
    if not value:
        return ""
    try:
        return _fernet().decrypt(value.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("Secret cannot be decrypted with current APP_SECRET") from exc


def decrypt_recoverable_api_key(encrypted_value: str | None) -> str:
    """解密可恢复 API 密钥；空值或当前应用密钥无法解密时抛出 ValueError。"""
    if not encrypted_value:
        raise ValueError("API key has no recoverable copy")
    return decrypt_secret(encrypted_value)


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "****"
    return f"{value[:3]}-****{value[-4:]}"
