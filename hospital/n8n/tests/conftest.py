"""Real n8n in a throw-away container (host network, own port), the flows imported and activated, every
downstream service replaced by the stub. Skipped when docker or the image is unavailable."""

import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

import pytest
from n8n_stub import Stub

HERE = Path(__file__).resolve().parent.parent
N8N_PORT, STUB_PORT = 5693, 5694
NAME = "claims-n8n-flowtest"
IMAGE = "n8nio/n8n:2.41.6"
SECRET = "test-webhook-secret"


def sh(*a: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(a, capture_output=True, text=True, check=check)  # noqa: S603


@pytest.fixture(scope="session")
def stub():  # type: ignore[no-untyped-def]
    s = Stub(STUB_PORT)
    s.start()
    yield s
    s.stop()


@pytest.fixture(scope="session")
def n8n(stub):  # type: ignore[no-untyped-def]
    if (
        not shutil.which("docker")
        or sh("docker", "image", "inspect", IMAGE, check=False).returncode
    ):
        pytest.skip("docker or the n8n image is not available")
    sh("docker", "rm", "-f", NAME, check=False)
    base = f"http://127.0.0.1:{STUB_PORT}"
    env = {
        "N8N_PORT": str(N8N_PORT), "N8N_BLOCK_ENV_ACCESS_IN_NODE": "false", "N8N_DIAGNOSTICS_ENABLED": "false",
        "N8N_ENCRYPTION_KEY": "test-encryption-key-0123456789abcdef", "N8N_WEBHOOK_SECRET": SECRET,
        "N8N_CLIENT_SECRET": "x", "HOSP_API_URL": base, "VISION_URL": base + "/vision", "DOCPIPE_URL": base + "/docpipe",
        "HOSP_CREW_URL": base + "/crew", "KEYCLOAK_TOKEN_URL": base + "/token", "INTAKE_MAX_PARSE_WAIT_S": "8",
        "N8N_RUNNERS_ENABLED": "true", "N8N_RUNNERS_BROKER_PORT": "5699", "N8N_SECURE_COOKIE": "false", "N8N_LOG_LEVEL": "warn",
    }  # fmt: skip
    ids = sorted(p.stem for p in (HERE / "flows").glob("*.json"))
    script = (
        "n8n import:workflow --separate --input=/flows && "
        + " && ".join(f"n8n publish:workflow --id={i}" for i in ids)
        + " && exec n8n start"
    )
    args = [
        "docker",
        "run",
        "-d",
        "--name",
        NAME,
        "--network",
        "host",
        "-v",
        f"{HERE / 'flows'}:/flows:ro",
    ]
    for k, v in env.items():
        args += ["-e", f"{k}={v}"]
    sh(*args, "--entrypoint", "sh", IMAGE, "-c", script)
    deadline = time.time() + 120
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{N8N_PORT}/healthz", timeout=2)  # noqa: S310
            break
        except Exception:  # noqa: BLE001
            time.sleep(2)
    else:
        logs = sh("docker", "logs", NAME, check=False)
        pytest.fail("n8n did not start:\n" + logs.stdout[-2000:] + logs.stderr[-2000:])
    time.sleep(3)  # let the webhooks register
    yield f"http://127.0.0.1:{N8N_PORT}"
    sh("docker", "rm", "-f", NAME, check=False)
