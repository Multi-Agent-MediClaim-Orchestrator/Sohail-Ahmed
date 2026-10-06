"""Schema-conformance run (T22): generated requests from claims-v1.yaml against the insurer->hospital
callbacks served by hospital_sim. Signatures are disabled here; signing is covered by test_middleware."""

import pathlib
import socket
import threading
import time

import pytest
import schemathesis
import uvicorn
from claim_contract.testing.hospital_sim import HospitalSim
from schemathesis.specs.openapi.checks import positive_data_acceptance

SPEC = pathlib.Path(__file__).resolve().parents[3] / "contract/openapi/claims-v1.yaml"
pytestmark = pytest.mark.contract


@pytest.fixture(scope="module")
def base_url():  # type: ignore[no-untyped-def]
    sim = HospitalSim(secrets={}, verify_signatures=False, accept_any_claim=True)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = uvicorn.Server(uvicorn.Config(sim.app, host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    t.join(timeout=5)


schema = schemathesis.openapi.from_path(str(SPEC)).include(tag="insurer->hospital")


@schema.parametrize()
def test_callbacks_conform_to_openapi(case, base_url):  # type: ignore[no-untyped-def]
    # built-in checks: no 5xx, documented status, content-type, response schema, missing/invalid input rejected.
    # positive_data_acceptance is excluded: models carry business validators (e.g. totals reconcile)
    # that a JSON-schema generator cannot satisfy, so 422 on schema-valid input is expected.
    resp = case.call_and_validate(base_url=base_url, excluded_checks=[positive_data_acceptance])
    assert resp.status_code in (200, 204, 400, 401, 403, 404, 405, 409, 413, 422, 429), (
        resp.status_code
    )
    if resp.status_code >= 400 and resp.status_code != 405:  # 405 comes from the router
        assert resp.headers["content-type"][0].startswith("application/problem+json")
        assert "code" in resp.json()
