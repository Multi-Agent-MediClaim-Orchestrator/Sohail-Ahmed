"""Fail on anything that looks like a real identifier: a Verhoeff-valid 12-digit number outside the reserved 9999 range."""

from __future__ import annotations

import subprocess
from pathlib import Path

from synth import ids


def scan_text(text: str) -> list[str]:
    return ids.real_looking_aadhaar(text)


def scan_dir(root: Path) -> list[tuple[str, str]]:
    bad: list[tuple[str, str]] = []
    for f in root.rglob("*"):
        if f.suffix == ".json":
            txt = f.read_text()
        elif f.suffix == ".pdf" and not f.name.endswith(".degraded.pdf"):
            txt = subprocess.run(
                ["pdftotext", str(f), "-"], capture_output=True, check=False
            ).stdout.decode(errors="ignore")  # noqa: S603, S607
        else:
            continue
        bad += [(str(f), m) for m in scan_text(txt)]
    return bad
