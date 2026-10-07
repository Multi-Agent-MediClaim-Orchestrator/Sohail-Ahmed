import json
import subprocess
import time
import urllib.error
import urllib.request

NAME = "claims-n8n-flowtest"
SECRET = "test-webhook-secret"


def sh(*a: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(a, capture_output=True, text=True, check=check)  # noqa: S603


def fire(
    n8n: str, path: str, body: dict, *, secret: str | None = SECRET, idem: str | None = None
) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json"}
    if secret is not None:
        headers["X-Webhook-Secret"] = secret
    if idem:
        headers["X-Idempotency-Key"] = idem
    req = urllib.request.Request(
        f"{n8n}/webhook/{path}", data=json.dumps(body).encode(), headers=headers, method="POST"
    )  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def run_flow(flow_id: str) -> None:
    """Run a cron flow once, in a second process, against the same database."""
    r = sh(
        "docker",
        "exec",
        "-e",
        "N8N_RUNNERS_BROKER_PORT=5696",
        "-e",
        "N8N_PORT=5697",
        NAME,
        "n8n",
        "execute",
        f"--id={flow_id}",
        check=False,
    )
    assert r.returncode == 0, r.stdout[-1500:] + r.stderr[-1500:]


def wait_for(cond, timeout: float = 30) -> bool:  # type: ignore[no-untyped-def]
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.5)
    return False
