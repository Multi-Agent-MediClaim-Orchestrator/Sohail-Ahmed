from datetime import UTC, datetime, timedelta

import pytest
from claim_contract.errors import InvalidSignature, StaleRequest
from claim_contract.signing import sign, verify

SECRET = b"test-secret-hosp-to-ins-0001"
V1 = (
    "POST",
    "/v1/hospital-api/claims",
    "2026-10-06T10:15:30Z",
    "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b",
    b'{"claim_ref":"HC-2026-000001"}',
)
V1_SIG = "JS6xvXXtuHpT2sltvMWlI/9EhupyXRohlMY+DXjc7qw="
V2 = (
    "GET",
    "/v1/hospital-api/claims/HC-2026-000001?include=queries",
    "2026-10-06T10:16:00Z",
    None,
    b"",
)
V2_SIG = "rSj5CoHCq3nAPK4xBnfvyitUn9J2zGKPlO+2pbRaiHM="
NOW = datetime(2026, 10, 6, 10, 15, 30, tzinfo=UTC)


def test_vectors() -> None:
    assert sign(SECRET, *V1) == V1_SIG
    assert sign(SECRET, *V2) == V2_SIG


def _verify(sig: str, args: tuple, now: datetime = NOW, key: str = "hosp-001") -> None:  # type: ignore[type-arg]
    verify({"hosp-001": SECRET}, key, sig, *args, now=now)


def test_ok_and_skew_edges() -> None:
    _verify(V1_SIG, V1)
    _verify(V1_SIG, V1, NOW + timedelta(seconds=299))
    _verify(V1_SIG, V1, NOW - timedelta(seconds=299))


@pytest.mark.parametrize("delta", [301, -301])
def test_stale(delta: int) -> None:
    with pytest.raises(StaleRequest):
        _verify(V1_SIG, V1, NOW + timedelta(seconds=delta))


def test_tampered_body_path_and_key() -> None:
    m, p, ts, i, b = V1
    with pytest.raises(InvalidSignature):
        _verify(V1_SIG, (m, p, ts, i, b + b" "))
    with pytest.raises(InvalidSignature):
        _verify(V1_SIG, (m, p + "?x=1", ts, i, b))
    with pytest.raises(InvalidSignature):
        _verify(V1_SIG, V1, key="unknown")
