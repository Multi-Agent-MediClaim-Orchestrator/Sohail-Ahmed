"""S5: replace each entity with a stable token (<PERSON_1>); the reversible map is AES-256-GCM encrypted at rest."""

from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from docpipe.stages.entities import Ent, find


@dataclass
class Masked:
    text: str
    pii_map: dict[str, str]  # token -> raw. Never logged, never sent anywhere.
    counts: dict[str, int]


def mask(text: str, ents: list[Ent] | None = None) -> Masked:
    ents = find(text) if ents is None else ents
    seen: dict[tuple[str, str], str] = {}
    n: dict[str, int] = {}
    pii: dict[str, str] = {}
    out: list[str] = []
    pos = 0
    for e in ents:
        raw = text[e.start : e.end]
        key = (e.type, re.sub(r"\s+", " ", raw.strip().lower()))
        if key not in seen:
            n[e.type] = n.get(e.type, 0) + 1
            seen[key] = f"<{e.type}_{n[e.type]}>"
            pii[seen[key]] = raw
        out += [text[pos : e.start], seen[key]]
        pos = e.end
    out.append(text[pos:])
    return Masked("".join(out), pii, {t: c for t, c in n.items()})


def unmask(text: str, pii: dict[str, str]) -> str:
    for tok in sorted(pii, key=len, reverse=True):
        text = text.replace(tok, pii[tok])
    return text


def _key(b64: str) -> bytes:
    if not b64:
        raise ValueError("DOCPIPE_PII_KEY_B64 is not set")
    k = base64.b64decode(b64)
    if len(k) != 32:
        raise ValueError("PII key must be 32 bytes")
    return k


def seal(pii: dict[str, str], b64key: str) -> bytes:
    nonce = os.urandom(12)
    return nonce + AESGCM(_key(b64key)).encrypt(nonce, json.dumps(pii).encode(), b"pii_map")


def open_(blob: bytes, b64key: str) -> dict[str, str]:
    return json.loads(AESGCM(_key(b64key)).decrypt(blob[:12], blob[12:], b"pii_map"))  # type: ignore[no-any-return]
