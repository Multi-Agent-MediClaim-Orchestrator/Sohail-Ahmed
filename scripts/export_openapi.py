"""Write hospital/api/openapi.json from the app (no database needed). `--check` fails if the committed file is stale.
Used by the UI's type generation and by a test."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "hospital" / "api"))
from app.core.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402

OUT = ROOT / "hospital" / "api" / "openapi.json"


def render() -> str:
    spec = create_app(Settings.from_env(outbox_enabled=False)).openapi()
    return json.dumps(spec, indent=1, sort_keys=True) + "\n"


if __name__ == "__main__":
    text = render()
    if "--check" in sys.argv:
        sys.exit(0 if OUT.exists() and OUT.read_text() == text else 1)
    OUT.write_text(text)
    print(f"wrote {OUT} ({len(text)} bytes)")
