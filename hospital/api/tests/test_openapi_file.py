import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_committed_openapi_is_current() -> None:
    """hospital/api/openapi.json feeds the UI types; regenerate with `uv run python scripts/export_openapi.py`."""
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "export_openapi.py"), "--check"],
        capture_output=True,
        check=False,
    )
    assert r.returncode == 0, "openapi.json is stale"
