"""AES-GCM field encryption for phone numbers (doc 01 §8): ciphertext = version byte | 12-byte nonce | data."""

from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

VERSION = 1


def _key(b64: str) -> bytes:
    k = base64.b64decode(b64)
    if len(k) != 32:
        raise ValueError("HOSP_FIELD_KEY must be 32 bytes (base64)")
    return k


def encrypt(plain: str, key_b64: str) -> bytes:
    nonce = os.urandom(12)
    return bytes([VERSION]) + nonce + AESGCM(_key(key_b64)).encrypt(nonce, plain.encode(), None)


def decrypt(blob: bytes, key_b64: str) -> str:
    if blob[0] != VERSION:
        raise ValueError(f"unknown field key version {blob[0]}")
    return AESGCM(_key(key_b64)).decrypt(blob[1:13], blob[13:], None).decode()
