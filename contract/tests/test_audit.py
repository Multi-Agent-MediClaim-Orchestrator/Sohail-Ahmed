import copy
from decimal import Decimal
from typing import Any

import pytest
from claim_contract import audit

CASE = "0199a1b2-0000-7000-8000-000000000001"
BASE = {
    "case_id": CASE,
    "actor_type": "system",
    "actor_id": "hospital-api",
    "config_versions": {"doc_requirements": 1},
    "model_info": None,
}
E1 = {
    **BASE,
    "seq": 1,
    "ts": "2026-10-06T10:00:00Z",
    "event_type": "case.created",
    "payload": {"claim_type": "cashless"},
}
E2 = {
    **BASE,
    "seq": 2,
    "ts": "2026-10-06T10:01:00Z",
    "event_type": "doc.uploaded",
    "payload": {"doc_id": "d1", "sha256": "ab" * 32},
}
H1 = "271ed01f81f1b1664abd152648469ac3d00c460ad241b0582e9da38510586165"
H2 = "faad23c74acb36c3d5c543e760ff855cb950c1c3a5c3683e7f3956ddba13b463"


class Mem:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.ev = events

    def events(self, case_id: str) -> list[dict[str, Any]]:
        return self.ev

    def head(self, case_id: str) -> tuple[int, str] | None:
        return (self.ev[-1]["seq"], self.ev[-1]["hash"]) if self.ev else None


def chain(n: int = 5) -> list[dict[str, Any]]:
    out, prev = [], audit.GENESIS
    for i in range(1, n + 1):
        e = {
            **BASE,
            "seq": i,
            "ts": "2026-10-06T10:00:00Z",
            "event_type": "doc.uploaded",
            "payload": {"i": i},
        }
        h = audit.compute_hash(prev, audit.event_body(e))
        out.append({**e, "prev_hash": prev, "hash": h, "journey_id": None})
        prev = h
    return out


def test_vectors() -> None:
    assert audit.compute_hash(audit.GENESIS, audit.event_body(E1)) == H1
    assert audit.compute_hash(H1, audit.event_body(E2)) == H2


def test_journey_id_does_not_change_hash() -> None:
    a = audit.compute_hash(audit.GENESIS, audit.event_body({**E1, "journey_id": "x"}))
    assert a == H1


def test_canonical_decimals_unicode_ordering() -> None:
    assert (
        audit.canonical_json({"b": Decimal("1.50"), "a": {"z": 1, "y": "é"}})
        == '{"a":{"y":"é","z":1},"b":"1.50"}'
    )


def test_verify_ok_and_tamper_modes() -> None:
    assert audit.verify_chain(Mem(chain()), CASE).ok
    ev = chain()
    ev[2]["payload"] = {"i": 99}
    r = audit.verify_chain(Mem(ev), CASE)
    assert (r.reason, r.broken_seq) == ("hash_mismatch", 3)

    ev = chain()
    del ev[2]
    assert audit.verify_chain(Mem(ev), CASE).reason == "gap_or_reorder"

    ev = chain()
    m = Mem(ev[:3])
    m.head = lambda c: (5, ev[4]["hash"])  # type: ignore[method-assign]
    assert audit.verify_chain(m, CASE).reason == "head_mismatch"

    ev = chain()
    ev[3]["prev_hash"] = "f" * 64
    assert audit.verify_chain(Mem(ev), CASE).reason == "prev_hash_mismatch"


def test_registry() -> None:
    audit.assert_known("claim.submitted")
    with pytest.raises(audit.UnknownEventType):
        audit.assert_known("claim.sumbitted")


@pytest.mark.parametrize(
    "raw",
    [
        "Aadhaar 1234 5678 9012 given",
        "PAN ABCDE1234F here",
        "call +91 9876543210 now",
        "mail asha.v@example.com ok",
        "9876543210",
    ],
)
def test_redaction_removes_pii(raw: str) -> None:
    out = audit.redact({"note": raw})["note"]
    assert not any(p.search(out) for p, _ in audit._PATTERNS)


def test_redaction_drops_keys_and_truncates() -> None:
    out = audit.redact({"raw_id": "1234 5678 9012", "text": "x" * 3000, "n": 1})
    assert "raw_id" not in out and "raw_id_sha256" in out and out["n"] == 1
    assert "truncated" in out["text"]
    assert copy.deepcopy(out)


def test_merkle_root_deterministic_and_order_independent() -> None:
    a = audit.merkle_root([("c1", "h1"), ("c2", "h2"), ("c3", "h3")])
    assert a == audit.merkle_root([("c3", "h3"), ("c1", "h1"), ("c2", "h2")])
    assert a != audit.merkle_root([("c1", "h1"), ("c2", "h2"), ("c3", "hX")])
