"""Idempotent seeding (`python -m seed.run`). Every insert is ON CONFLICT DO NOTHING on a natural
key, so running twice yields identical row counts. Uses the owner role: config is published here
through the same checksum rules the admin API will use."""

import hashlib
import json
import os
import pathlib
import random
import sys
from datetime import date, timedelta

import uuid_utils
from app.db.urls import _env, owner_url
from claim_contract.audit import canonical_json
from claim_contract.transitions import HOSPITAL_TRANSITIONS
from faker import Faker
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection

from seed.config_payloads import DOMAINS

HERE = pathlib.Path(__file__).parent
NETWORK = [
    ("Acme Health", True),
    ("Bharat Care", True),
    ("Zenith Mutual", False),
    ("Orbit TPA", True),
]


def uid() -> str:
    return str(uuid_utils.uuid7())


def seed_hospital(c: Connection) -> None:
    c.execute(
        text(
            "INSERT INTO hospital (id, code, name, nabh_accredited, hmac_key_id) "
            "VALUES (:id, 'HOSP-0001', 'Demo General Hospital', true, :k) ON CONFLICT (code) DO NOTHING"
        ),
        {"id": uid(), "k": _env().get("HOSP_KEY_ID", "hosp-001")},
    )


def seed_users(c: Connection) -> None:
    for u in json.loads((HERE / "users.json").read_text()):
        c.execute(
            text(
                "INSERT INTO app_user (id, keycloak_sub, email, display_name, role) "
                "VALUES (:id, :sub, :email, :name, :role) ON CONFLICT (keycloak_sub) DO NOTHING"
            ),
            {
                "id": uid(),
                "sub": u["sub"],
                "email": u["email"],
                "name": u["display_name"],
                "role": u["role"],
            },
        )


def seed_transitions(c: Connection) -> None:
    for a, targets in HOSPITAL_TRANSITIONS.items():
        for b in sorted(targets):
            c.execute(
                text(
                    "INSERT INTO allowed_transition VALUES (CAST(:a AS hospital_case_status), "
                    "CAST(:b AS hospital_case_status)) ON CONFLICT DO NOTHING"
                ),
                {"a": a.value, "b": b.value},
            )


def seed_network(c: Connection) -> None:
    for name, ok in NETWORK:
        c.execute(
            text(
                "INSERT INTO network_insurer (insurer_name, cashless_supported) VALUES (:n, :o) "
                "ON CONFLICT DO NOTHING"
            ),
            {"n": name, "o": ok},
        )


def seed_patients(c: Connection) -> list[tuple[str, str, str]]:
    """20 synthetic patients, one policy ref each. Returns (member_id, insurer, policy_no)."""
    fake = Faker("en_IN")
    Faker.seed(42)
    rnd = random.Random(42)
    pepper = _env().get("HOSP_ID_PEPPER", "dev-pepper")
    refs = []
    for i in range(1, 21):
        uhid = f"UHID-{i:06d}"
        raw = "".join(rnd.choice("0123456789") for _ in range(12))  # fake Aadhaar, never stored
        pid = uid()
        c.execute(
            text(
                "INSERT INTO patient (id, uhid, full_name, dob, gender, id_proof_type, id_proof_hash, id_proof_last4) "
                "VALUES (:id, :uhid, :name, :dob, :g, 'aadhaar', :h, :l4) ON CONFLICT (uhid) DO NOTHING"
            ),
            {
                "id": pid,
                "uhid": uhid,
                "name": fake.name(),
                "dob": date(1950, 1, 1) + timedelta(days=rnd.randint(0, 365 * 60)),
                "g": rnd.choice("MF"),
                "h": hashlib.sha256((pepper + raw).encode()).hexdigest(),
                "l4": raw[-4:],
            },
        )
        real = c.execute(text("SELECT id FROM patient WHERE uhid = :u"), {"u": uhid}).scalar_one()
        insurer, _ = NETWORK[i % len(NETWORK)]
        member, policy = f"MEM-{77120000 + i:08d}", f"POL-NIV-2026-{4400 + i:06d}"
        c.execute(
            text(
                "INSERT INTO insurance_policy_ref (id, patient_id, insurer_name, policy_number, member_id, "
                "valid_from, valid_to) VALUES (:id, :p, :ins, :pol, :m, '2026-01-01', '2026-12-31') "
                "ON CONFLICT ON CONSTRAINT uq_policy_ref DO NOTHING"
            ),
            {"id": uid(), "p": real, "ins": insurer, "pol": policy, "m": member},
        )
        refs.append((member, insurer, policy))
    return refs


def seed_preauth(c: Connection, refs: list[tuple[str, str, str]]) -> None:
    rows = []
    for n, (member, insurer, _) in enumerate(refs[:8], 1):  # healthy approved pre-auths
        rows.append(
            (
                f"PA-2026-{33000 + n}",
                member,
                insurer,
                150000 + n * 10000,
                "2026-01-01",
                "2026-12-31",
                "approved",
            )
        )
    m9, i9, _ = refs[8]
    rows += [
        (
            "PA-2026-33101",
            "MEM-99999999",
            i9,
            100000,
            "2026-01-01",
            "2026-12-31",
            "approved",
        ),  # member mismatch
        ("PA-2026-33102", m9, i9, 80000, "2025-01-01", "2025-06-30", "expired"),  # expired
        (
            "PA-2026-33103",
            refs[9][0],
            refs[9][1],
            20000,
            "2026-01-01",
            "2026-12-31",
            "approved",
        ),  # low amount
        (
            "PA-2026-33104",
            refs[10][0],
            refs[10][1],
            120000,
            "2026-01-01",
            "2026-12-31",
            "cancelled",
        ),
    ]
    for r in rows:
        c.execute(
            text(
                "INSERT INTO simulated_preauth (ref, member_id, insurer_name, approved_amount, valid_from, "
                "valid_to, status) VALUES (:a, :b, :c, :d, :e, :f, :g) ON CONFLICT DO NOTHING"
            ),
            dict(zip("abcdefg", r, strict=True)),
        )


def seed_config(c: Connection) -> None:
    for domain, payload in DOMAINS.items():
        c.execute(
            text(
                "INSERT INTO config_set (id, domain, name, description, created_by) "
                "VALUES (:id, :d, 'default', :desc, 'seed') ON CONFLICT (domain, name) DO NOTHING"
            ),
            {"id": uid(), "d": domain, "desc": f"v1 {domain}"},
        )
        set_id = c.execute(
            text("SELECT id FROM config_set WHERE domain=:d AND name='default'"), {"d": domain}
        ).scalar_one()
        checksum = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
        c.execute(
            text(
                "INSERT INTO config_version (id, config_set_id, version, status, payload, payload_schema, "
                "checksum, effective_from, change_note, created_by, published_by, published_at) "
                "VALUES (:id, :s, 1, 'published', CAST(:p AS jsonb), :schema, :ck, '2026-01-01T00:00:00Z', "
                "'seed v1', 'seed', 'seed', now()) ON CONFLICT (config_set_id, version) DO NOTHING"
            ),
            {
                "id": uid(),
                "s": set_id,
                "p": json.dumps(payload),
                "schema": f"{domain}@1",
                "ck": checksum,
            },
        )


def run(url: str | None = None) -> None:
    eng = create_engine(url or owner_url())
    with eng.begin() as c:
        seed_transitions(c)
        seed_hospital(c)
        seed_users(c)
        seed_network(c)
        refs = seed_patients(c)
        seed_preauth(c, refs)
        seed_config(c)
    print("seed done", file=sys.stderr)


if __name__ == "__main__":
    run(os.environ.get("HOSP_DB_OWNER_URL"))
