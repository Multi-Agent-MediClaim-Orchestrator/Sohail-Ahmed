"""Versioned prompt files: prompts/<name>/v<N>.md with a small front matter. The version used is recorded in
every output (and so in the audit trail). CREW_PROMPT_PIN_<NAME>=v1 pins a version for rollbacks."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Prompt:
    name: str
    version: str
    meta: dict
    body: str

    def render(self, **kw: str) -> str:
        out = self.body
        for k, v in kw.items():
            out = out.replace("{" + k + "}", v)
        return out


def load(prompt_dir: Path, name: str, pins: dict[str, str] | None = None) -> Prompt:
    d = prompt_dir / name
    ver = (pins or {}).get(name)
    if ver is None:
        nums = sorted(int(p.stem[1:]) for p in d.glob("v*.md") if p.stem[1:].isdigit())
        if not nums:
            raise FileNotFoundError(f"no prompt versions for {name}")
        ver = f"v{nums[-1]}"
    raw = (d / f"{ver}.md").read_text()
    meta: dict = {}
    body = raw
    if raw.startswith("---\n"):
        _, fm, body = raw.split("---\n", 2)
        meta = yaml.safe_load(fm) or {}
    return Prompt(name, ver, meta, body.strip())
