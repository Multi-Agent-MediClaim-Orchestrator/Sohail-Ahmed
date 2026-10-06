"""Merge missing keys from .env.example into .env and fill __GENERATE__/__KMSKEY__
placeholders with random secrets. Idempotent: existing values are never changed."""

import base64
import os
import pathlib
import secrets

env, example = pathlib.Path(".env"), pathlib.Path(".env.example")
have = {ln.split("=", 1)[0] for ln in env.read_text().splitlines() if "=" in ln}
lines = env.read_text().splitlines()
for ln in example.read_text().splitlines():
    if "=" in ln and not ln.startswith("#") and ln.split("=", 1)[0] not in have:
        lines.append(ln)
out = []
for ln in lines:
    if "__GENERATE__" in ln:
        ln = ln.replace("__GENERATE__", secrets.token_hex(16))
    if "__KMSKEY__" in ln:
        ln = ln.replace("__KMSKEY__", base64.b64encode(os.urandom(32)).decode())
    out.append(ln)
env.write_text("\n".join(out) + "\n")
