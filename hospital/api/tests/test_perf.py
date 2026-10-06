"""Performance smoke (01-hospital-db §9). Marked slow; run with `pytest -m slow`."""

import hashlib
import statistics
import time
import uuid

import pytest
import uuid_utils
from app.db.urls import owner_url
from claim_contract.audit import GENESIS, compute_hash, event_body
from sqlalchemy import create_engine, text

pytestmark = [pytest.mark.integration, pytest.mark.slow]


def test_100k_audit_inserts_with_hash_chain_under_60s(migrated: str) -> None:
    eng = create_engine(owner_url(migrated))
    case = str(uuid.uuid4())
    rows, prev = [], GENESIS
    t0 = time.perf_counter()
    for seq in range(1, 100_001):
        e = {
            "seq": seq,
            "case_id": case,
            "ts": "2026-10-06T10:00:00Z",
            "actor_type": "system",
            "actor_id": "perf",
            "event_type": "doc.uploaded",
            "payload": {"i": seq},
            "config_versions": {},
            "model_info": None,
        }
        h = compute_hash(prev, event_body(e))
        rows.append({"id": str(uuid_utils.uuid7()), "seq": seq, "case": case, "p": prev, "h": h})
        prev = h
    with eng.begin() as c:
        for i in range(0, len(rows), 5000):
            c.execute(
                text(
                    "INSERT INTO audit_event (id, seq, case_id, ts, actor_type, actor_id, event_type, payload, "
                    "prev_hash, hash) VALUES (:id, :seq, :case, '2026-10-06T10:00:00Z', 'system', 'perf', "
                    "'doc.uploaded', '{}', :p, :h)"
                ),
                rows[i : i + 5000],
            )
    assert time.perf_counter() - t0 < 60


def test_case_list_keyset_uses_index_and_p95_under_150ms(migrated: str) -> None:
    eng = create_engine(owner_url(migrated))
    with eng.begin() as c:
        c.execute(
            text(
                "INSERT INTO hospital (id, code, name, hmac_key_id) VALUES (uuid_generate_v7(), 'PERF', 'p', 'k') ON CONFLICT DO NOTHING"
            )
        )
        c.execute(
            text(
                "INSERT INTO app_user (id, keycloak_sub, email, display_name, role) VALUES (uuid_generate_v7(), 'perf', 'perf@x.io', 'p', 'desk') ON CONFLICT DO NOTHING"
            )
        )
        c.execute(
            text(
                "INSERT INTO patient (id, uhid, full_name, dob, gender) VALUES (uuid_generate_v7(), 'PERF-1', 'Perf Pat', '1980-01-01', 'F') ON CONFLICT DO NOTHING"
            )
        )
        c.execute(
            text(
                "INSERT INTO insurance_policy_ref (id, patient_id, insurer_name, policy_number, member_id) SELECT uuid_generate_v7(), id, 'Acme Health', 'PP', 'MM' FROM patient WHERE uhid='PERF-1' ON CONFLICT DO NOTHING"
            )
        )
        c.execute(
            text(
                "INSERT INTO claim_case (id, claim_ref, hospital_id, patient_id, policy_ref_id, claim_type, admission_type, created_by, created_at) "
                "SELECT uuid_generate_v7(), 'PERF-' || g, h.id, p.id, r.id, 'cashless', 'planned', u.id, now() - (g || ' seconds')::interval "
                "FROM generate_series(1, 10000) g, hospital h, patient p, insurance_policy_ref r, app_user u "
                "WHERE h.code='PERF' AND p.uhid='PERF-1' AND r.member_id='MM' AND u.keycloak_sub='perf'"
            )
        )
        c.execute(text("ANALYZE claim_case"))
    q = (
        "SELECT id FROM claim_case WHERE (created_at, id) < (now(), 'ffffffff-ffff-ffff-ffff-ffffffffffff') "
        "ORDER BY created_at DESC, id DESC LIMIT 50"
    )
    with eng.connect() as c:
        plan = "\n".join(r[0] for r in c.execute(text("EXPLAIN " + q)))
        assert "Seq Scan" not in plan and "ix_case_created_keyset" in plan, plan
        times = []
        for _ in range(50):
            t = time.perf_counter()
            c.execute(text(q)).all()
            times.append((time.perf_counter() - t) * 1000)
    assert statistics.quantiles(times, n=20)[18] < 150  # p95
    assert hashlib.sha256(b"x")  # keep import used
