"""Presigned URLs are bearer credentials: stored Fernet-encrypted, never logged (02 §5 task 15)."""

from __future__ import annotations

from cryptography.fernet import Fernet

from ..settings import get_settings

_dev_key: bytes | None = None


def _fernet() -> Fernet:
    global _dev_key
    key = get_settings().url_enc_key
    if key:
        return Fernet(key.encode())
    if _dev_key is None:  # per-process key in dev: URLs do not survive a restart (re-fetch via refresh-url)
        _dev_key = Fernet.generate_key()
    return Fernet(_dev_key)


def encrypt_url(url: str) -> str:
    return _fernet().encrypt(url.encode()).decode()


def decrypt_url(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()
