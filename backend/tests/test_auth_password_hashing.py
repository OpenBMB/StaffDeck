import base64
import hashlib

from app.security.auth import hash_password, verify_password


def _legacy_hash(password: str, app_secret: str = "test-secret-key-for-legacy") -> str:
    """Replicate the old hash_password() that derived salt from APP_SECRET."""
    salt = hashlib.sha256(app_secret.encode("utf-8")).hexdigest()[:16]
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000)
    return f"pbkdf2_sha256${salt}${base64.urlsafe_b64encode(digest).decode('utf-8')}"


def test_same_password_produces_different_hashes() -> None:
    h1 = hash_password("mypassword")
    h2 = hash_password("mypassword")
    assert h1 != h2


def test_correct_password_accepted_and_wrong_rejected() -> None:
    stored = hash_password("correcthorse")
    assert verify_password("correcthorse", stored) is True
    assert verify_password("wrongpassword", stored) is False


def test_legacy_hash_verifies_with_new_verify() -> None:
    old_hash = _legacy_hash("oldpassword")
    assert verify_password("oldpassword", old_hash) is True
    assert verify_password("wrongpassword", old_hash) is False


def test_malformed_hash_returns_false() -> None:
    assert verify_password("anypassword", "not-a-valid-hash") is False
    assert verify_password("anypassword", "") is False
