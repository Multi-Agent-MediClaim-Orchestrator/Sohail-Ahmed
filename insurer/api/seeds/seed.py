"""Deterministic, idempotent seed of master data + config + users (01-insurer-db §6.6).

    python -m seeds.seed            # uses INS_OWNER_DATABASE_URL (or INS_DATABASE_URL)
Re-running never changes counts: natural-key upserts (policy_number, member_id, hospital_code, code)."""

from __future__ import annotations

import hashlib
import json
import os
import random
import sys
import uuid
from datetime import date, timedelta
from pathlib import Path

from app.config.defaults import DOC_REQUIREMENTS, PRODUCTS, QUERY_POLICY, THRESHOLDS, policy_rules
from app.config.schemas import DOMAIN_SCHEMA, SCHEMAS
from app.config.service import checksum_of
from app.verification.names import normalise_name
from claim_contract import audit
from sqlalchemy import create_engine, text

SEED = 42
ID_SALT = "synthetic-salt"
EXPORT = Path(__file__).parent / "export"
EFFECTIVE = "2026-01-01T00:00:00+00:00"


def det_uuid(kind: str, key: str) -> str:
    """Deterministic UUID so re-seeding keeps primary keys stable (uuid5 over a fixed namespace)."""
    return str(uuid.uuid5(uuid.UUID("0199a1b2-0000-7000-8000-00000000cafe"), f"{kind}:{key}"))


def id_hash(member_id: str) -> str:
    return hashlib.sha256(f"{ID_SALT}|ID-{member_id}".encode()).hexdigest()


FIRST = ["Asha", "Ravi", "Priya", "Arjun", "Sneha", "Vikram", "Meera", "Rohan", "Anita", "Karthik", "Divya", "Suresh", "Neha", "Amit",
         "Pooja", "Sanjay", "Lakshmi", "Rahul", "Kavita", "Manoj", "Deepa", "Anil", "Sunita", "Prakash", "Rekha", "Vijay"]
LAST = ["Verma", "Kumar", "Sharma", "Iyer", "Nair", "Reddy", "Patel", "Singh", "Gupta", "Menon", "Das", "Joshi", "Rao", "Mishra",
        "Bose", "Khan", "Pillai", "Chopra", "Desai", "Kulkarni"]
PRE_EXISTING = [["E11"], ["I10"], ["J45"], ["E11", "I10"], ["N18"]]


def build_master() -> dict[str, list[dict]]:
    r = random.Random(SEED)
    products = [{"id": det_uuid("product", c), "code": c, "name": v["name"], "insurer_name": "Niveshak Health Insurance (synthetic)"} for c, v in PRODUCTS.items()]
    codes = list(PRODUCTS)
    statuses = ["active"] * 45 + ["lapsed"] * 5 + ["suspended"] * 5 + ["grace"] * 5  # 60 policies
    r.shuffle(statuses)
    statuses[0] = "active"  # persona policy
    policies, members = [], []
    sizes = ([2, 4, 3, 3, 2, 4] * 10)
    sizes[0], sizes[1] = 1, 5  # persona policy has one member; keeps the total at exactly 180
    member_no = 0
    for i in range(60):
        pno = "POL-NIV-2025-004411" if i == 0 else f"POL-NIV-2025-{i + 100:06d}"
        code = "HEALTH-PLUS-GOLD" if i == 0 else codes[i % 3]
        si = 500000 if i == 0 else r.choice([300000, 500000, 1000000, 2500000])
        st = statuses[i]
        start = date(2026, 1, 1) + timedelta(days=(i * 5) % 200)
        end = date(start.year + 1, start.month, min(start.day, 28)) - timedelta(days=1)
        paid_until = end
        status = "active"
        if st == "lapsed":
            status, paid_until = "lapsed", start + timedelta(days=60)
        elif st == "suspended":
            status = "suspended"
        elif st == "grace":
            status, paid_until = "lapsed", date(2026, 9, 20)  # within 30-day grace for admissions up to 2026-10-20
        policies.append({"id": det_uuid("policy", pno), "policy_number": pno, "product_id": det_uuid("product", code), "policy_holder_name": f"{r.choice(FIRST)} {r.choice(LAST)}",
                         "start_date": start.isoformat(), "end_date": end.isoformat(), "sum_insured": si, "status": status,
                         "premium_paid_until": paid_until.isoformat(), "grace_days": 30})
        n = sizes[i]
        for j in range(n):
            member_no += 1
            mid = "MEM-77120345" if (i == 0 and j == 0) else f"MEM-{70000000 + member_no:08d}"
            rel = "self" if j == 0 else r.choice(["spouse", "child", "child", "parent"])
            dob = date(1984, 3, 12) if mid == "MEM-77120345" else date(1940, 1, 1) + timedelta(days=r.randint(0, 80 * 365))
            name = "Asha Verma" if mid == "MEM-77120345" else f"{r.choice(FIRST)} {r.choice(LAST)}"
            pre = r.choice(PRE_EXISTING) if (mid != "MEM-77120345" and r.random() < 0.15) else []
            members.append({"id": det_uuid("member", mid), "policy_id": det_uuid("policy", pno), "member_id": mid, "full_name": name,
                            "full_name_norm": normalise_name(name), "dob": dob.isoformat(), "gender": "F" if mid == "MEM-77120345" else r.choice("MF"),
                            "relationship": rel, "id_proof_hash": id_hash(mid), "cover_start": (start - timedelta(days=365) if (member_no % 2 == 0 or mid == "MEM-77120345") else start).isoformat(), "pre_existing": pre})
    # trim/extend to exactly 180 members
    assert len(members) == 180, len(members)
    hospitals = []
    for k in range(1, 13):
        code = f"HOSP-{k:04d}"
        status = "network" if k <= 8 else ("non_network" if k <= 11 else "blacklisted")
        hospitals.append({"id": det_uuid("hospital", code), "hospital_code": code, "name": f"{r.choice(['Sunrise', 'Apollo', 'Fortis', 'Lifeline', 'City Care', 'Medicover', 'Sanjeevani', 'Metro'])} Hospital {k}",
                          "city": r.choice(["Pune", "Bengaluru", "Chennai", "Mumbai", "Hyderabad", "Delhi"]), "network_status": status,
                          "room_rent_tier": r.choice("ABC"), "empanelment_valid_till": "2027-12-31", "hmac_key_id": f"hosp-{k:03d}",
                          "callback_base_url": os.environ.get("INS_HOSPITAL_CALLBACK_BASE", "http://localhost:8000"),
                          "account_hash": hashlib.sha256(f"SIM-ACCOUNT-{code}".encode()).hexdigest(), "watchlist": k == 8})
    users = [
        ("reviewer1", "Riya Reviewer", ["reviewer"], ["cashless", "reimbursement"]), ("reviewer2", "Rahul Reviewer", ["reviewer"], ["cashless"]),
        ("reviewer3", "Rohit Reviewer", ["reviewer"], ["reimbursement", "high_value"]), ("senior1", "Sonia Senior", ["reviewer", "senior_reviewer", "approver"], ["cashless", "reimbursement", "high_value", "senior"]),
        ("senior2", "Sanjay Senior", ["reviewer", "senior_reviewer", "approver"], ["cashless", "high_value", "senior"]),
        ("approver1", "Anita Approver", ["approver"], []), ("approver2", "Arun Approver", ["approver"], []), ("admin1", "Ajay Admin", ["admin"], []),
    ]
    return {"products": products, "policies": policies, "members": members, "hospitals": hospitals,
            "users": [{"sub": s, "display_name": n, "email": f"{s}@insurer.local", "roles": roles, "skill_tags": tags} for s, n, roles, tags in users]}


def seed(url: str) -> dict[str, int]:
    if os.environ.get("INS_ENV", "dev") == "prod":
        raise SystemExit("refusing to seed when INS_ENV=prod")
    data = build_master()
    eng = create_engine(url.replace("postgresql+asyncpg://", "postgresql+psycopg2://").replace("postgresql://", "postgresql+psycopg2://", 1))
    with eng.begin() as c:
        for p in data["products"]:
            c.execute(text("INSERT INTO core.insurance_product (id, code, name, insurer_name) VALUES (:id,:code,:name,:insurer_name) "
                           "ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name"), p)
        for p in data["policies"]:
            c.execute(text("INSERT INTO core.policy (id, policy_number, product_id, policy_holder_name, start_date, end_date, sum_insured, status, premium_paid_until, grace_days) "
                           "VALUES (:id,:policy_number,:product_id,:policy_holder_name,:start_date,:end_date,:sum_insured,:status,:premium_paid_until,:grace_days) "
                           "ON CONFLICT (policy_number) DO UPDATE SET status = EXCLUDED.status, premium_paid_until = EXCLUDED.premium_paid_until, sum_insured = EXCLUDED.sum_insured"), p)
        for m in data["members"]:
            c.execute(text("INSERT INTO core.policy_member (id, policy_id, member_id, full_name, full_name_norm, dob, gender, relationship, id_proof_hash, cover_start, pre_existing) "
                           "VALUES (:id,:policy_id,:member_id,:full_name,:full_name_norm,:dob,:gender,:relationship,:id_proof_hash,:cover_start,:pre_existing) "
                           "ON CONFLICT (member_id) DO UPDATE SET full_name = EXCLUDED.full_name, pre_existing = EXCLUDED.pre_existing"), m)
        for h in data["hospitals"]:
            c.execute(text("INSERT INTO core.network_hospital (id, hospital_code, name, city, network_status, room_rent_tier, empanelment_valid_till, hmac_key_id, callback_base_url, account_hash, watchlist) "
                           "VALUES (:id,:hospital_code,:name,:city,:network_status,:room_rent_tier,:empanelment_valid_till,:hmac_key_id,:callback_base_url,:account_hash,:watchlist) "
                           "ON CONFLICT (hospital_code) DO UPDATE SET network_status = EXCLUDED.network_status, callback_base_url = EXCLUDED.callback_base_url"), h)
        for u in data["users"]:
            c.execute(text("INSERT INTO ops.user_profile (sub, display_name, email, roles, skill_tags) VALUES (:sub,:display_name,:email,:roles,:skill_tags) "
                           "ON CONFLICT (sub) DO UPDATE SET roles = EXCLUDED.roles, skill_tags = EXCLUDED.skill_tags"), u)
        _seed_config(c)
    EXPORT.mkdir(exist_ok=True)
    (EXPORT / "members.json").write_text(json.dumps({"policies": data["policies"], "members": data["members"], "hospitals": [
        {k: v for k, v in h.items() if k != "account_hash"} for h in data["hospitals"]]}, indent=1), encoding="utf-8")
    return {k: len(v) for k, v in data.items()}


def _publish(c, domain: str, name: str, payload: dict) -> None:
    schema_id = DOMAIN_SCHEMA[domain]
    model = SCHEMAS[schema_id].model_validate({"schema_id": schema_id, **payload})
    body = json.loads(model.model_dump_json(by_alias=True))
    sid = det_uuid("cfgset", f"{domain}/{name}")
    c.execute(text("INSERT INTO config.config_set (id, domain, name, created_by) VALUES (:i,:d,:n,'seed') ON CONFLICT (domain, name) DO NOTHING"),
              {"i": sid, "d": domain, "n": name})
    set_id = c.execute(text("SELECT id FROM config.config_set WHERE domain=:d AND name=:n"), {"d": domain, "n": name}).scalar_one()
    c.execute(text("INSERT INTO config.config_version (id, config_set_id, version, status, payload, payload_schema, checksum, effective_from, change_note, created_by, published_by, second_approver, published_at) "
                   "VALUES (:i,:s,1,'published',CAST(:p AS JSONB),:ps,:ck,:ef,'seed v1','seed','seed','seed-2',now()) ON CONFLICT (config_set_id, version) DO NOTHING"),
              {"i": det_uuid("cfgver", f"{domain}/{name}/1"), "s": set_id, "p": json.dumps(body), "ps": schema_id, "ck": checksum_of(body), "ef": EFFECTIVE})


def _seed_config(c) -> None:
    _publish(c, "thresholds", "default", THRESHOLDS)
    _publish(c, "query_policy", "default", QUERY_POLICY)
    _publish(c, "doc_requirements", "default", DOC_REQUIREMENTS)
    for code in PRODUCTS:
        _publish(c, "policy_rules", code, policy_rules(code))
    _publish(c, "policy_rules", "default", policy_rules("HEALTH-PLUS-GOLD"))
    _ = audit  # config.published audit events are written by the admin workflow, not by the seed


if __name__ == "__main__":
    url = os.environ.get("INS_OWNER_DATABASE_URL") or os.environ.get("INS_DATABASE_URL")
    if not url:
        sys.exit("set INS_OWNER_DATABASE_URL")
    print(seed(url))
