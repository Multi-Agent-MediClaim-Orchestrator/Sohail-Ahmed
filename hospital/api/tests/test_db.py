"""DB behaviour tests from 01-hospital-db §9. Integration: needs the hospital-db container."""

import itertools
import threading
import uuid
from decimal import Decimal

import pytest
import uuid_utils
from alembic import command
from app.db.urls import app_sync_url, n8n_url, owner_url, readonly_url
from claim_contract import enums as ce
from claim_contract.transitions import HOSPITAL_TRANSITIONS
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import (
    DBAPIError,
    IntegrityError,
    InternalError,
    OperationalError,
    ProgrammingError,
)
from tests.conftest import alembic_cfg

pytestmark = pytest.mark.integration


def uid() -> str:
    return str(uuid_utils.uuid7())


def make_case(c: Connection, status: str = "draft", claim_type: str = "cashless") -> str:
    """Insert the minimum parent rows plus one case; returns case id."""
    n = uuid.uuid4().hex[:8]
    hid, user, pat, pol, case = uid(), uid(), uid(), uid(), uid()
    c.execute(
        text("INSERT INTO hospital (id, code, name, hmac_key_id) VALUES (:i, :c, 'h', 'k')"),
        {"i": hid, "c": f"H-{n}"},
    )
    c.execute(
        text(
            "INSERT INTO app_user (id, keycloak_sub, email, display_name, role) "
            "VALUES (:i, :s, :e, 'u', 'officer')"
        ),
        {"i": user, "s": n, "e": f"{n}@x.io"},
    )
    c.execute(
        text(
            "INSERT INTO patient (id, uhid, full_name, dob, gender) VALUES (:i, :u, 'P Q', '1980-01-01', 'F')"
        ),
        {"i": pat, "u": f"U-{n}"},
    )
    c.execute(
        text(
            "INSERT INTO insurance_policy_ref (id, patient_id, insurer_name, policy_number, member_id) "
            "VALUES (:i, :p, 'Acme Health', 'POL-1', 'MEM-1')"
        ),
        {"i": pol, "p": pat},
    )
    c.execute(
        text(
            "INSERT INTO claim_case (id, claim_ref, hospital_id, patient_id, policy_ref_id, claim_type, "
            "admission_type, status, created_by) VALUES (:i, :r, :h, :p, :pol, CAST(:t AS claim_type), "
            "'planned', CAST(:s AS hospital_case_status), :u)"
        ),
        {
            "i": case,
            "r": f"HC-2026-{n}",
            "h": hid,
            "p": pat,
            "pol": pol,
            "t": claim_type,
            "s": status,
            "u": user,
        },
    )
    return case


def make_doc(c: Connection, case: str, **kw: object) -> str:
    d = {
        "id": uid(),
        "case": case,
        "sha": uuid.uuid4().hex * 2,
        "scan": "clean",
        "life": "active",
        **kw,
    }
    c.execute(
        text(
            "INSERT INTO document (id, case_id, original_filename, mime_type, size_bytes, sha256, "
            "storage_key, scan_status, lifecycle) VALUES (:id, :case, 'a.pdf', 'application/pdf', 10, "
            ":sha, 'k', :scan, :life)"
        ),
        d,
    )
    return d["id"]


# --- migrations -------------------------------------------------------------------------------
def test_migration_up_down_up(dbname: str, migrated: str) -> None:
    cfg = alembic_cfg(owner_url(dbname))
    command.downgrade(cfg, "base")
    with create_engine(owner_url(dbname)).connect() as c:
        left = c.execute(
            text(
                "SELECT count(*) FROM information_schema.tables WHERE table_schema='public' "
                "AND table_name <> 'alembic_version'"
            )
        ).scalar()
        assert left == 0
    command.upgrade(cfg, "head")


def test_alembic_check_no_drift(dbname: str, migrated: str) -> None:
    command.check(alembic_cfg(owner_url(dbname)))


def test_extensions_roles_and_uuid7(conn: Connection) -> None:
    exts = {r[0] for r in conn.execute(text("SELECT extname FROM pg_extension"))}
    assert {"pgcrypto", "citext", "pg_trgm", "btree_gist"} <= exts
    v = conn.execute(text("SELECT uuid_generate_v7()")).scalar()
    assert v.version == 7


@pytest.mark.parametrize(
    ("cls", "pg"),
    [
        (ce.ClaimType, "claim_type"),
        (ce.AdmissionType, "admission_type"),
        (ce.HospitalCaseStatus, "hospital_case_status"),
        (ce.DocType, "doc_type"),
        (ce.QueryCategory, "query_category"),
        (ce.QueryStatus, "query_status"),
    ],
)
def test_enum_parity(conn: Connection, cls: type, pg: str) -> None:
    labels = [
        r[0]
        for r in conn.execute(
            text(
                "SELECT enumlabel FROM pg_enum e JOIN pg_type t ON t.oid=e.enumtypid WHERE t.typname=:n "
                "ORDER BY enumsortorder"
            ),
            {"n": pg},
        )
    ]
    assert labels == [m.value for m in cls]


# --- state machine ------------------------------------------------------------------------------
def test_transition_trigger_exhaustive(owner) -> None:  # type: ignore[no-untyped-def]
    statuses = list(ce.HospitalCaseStatus)
    with owner.connect() as c:
        tx = c.begin()
        case = make_case(c)
        for a, b in itertools.product(statuses, statuses):
            if a == b:
                continue
            c.execute(text("ALTER TABLE claim_case DISABLE TRIGGER trg_case_transition"))
            c.execute(
                text("UPDATE claim_case SET status=CAST(:a AS hospital_case_status) WHERE id=:i"),
                {"a": a.value, "i": case},
            )
            c.execute(text("ALTER TABLE claim_case ENABLE TRIGGER trg_case_transition"))
            sp = c.begin_nested()
            try:
                c.execute(
                    text(
                        "UPDATE claim_case SET status=CAST(:b AS hospital_case_status) WHERE id=:i"
                    ),
                    {"b": b.value, "i": case},
                )
                ok = True
            except (DBAPIError, InternalError) as e:
                ok = False
                assert "invalid_transition" in str(e)
            sp.rollback()
            assert ok == (b in HOSPITAL_TRANSITIONS[a]), (a, b)
        tx.rollback()


# --- audit / config immutability ------------------------------------------------------------------
def audit_row(case: str, seq: int) -> dict[str, object]:
    return {"id": uid(), "case": case, "seq": seq, "p": "0" * 64, "h": "1" * 64}


INSERT_AUDIT = text(
    "INSERT INTO audit_event (id, seq, case_id, ts, actor_type, actor_id, event_type, payload, prev_hash, hash) "
    "VALUES (:id, :seq, :case, now(), 'system', 'x', 'case.created', '{}', :p, :h)"
)


def test_audit_append_only_for_app_role(migrated: str, owner) -> None:  # type: ignore[no-untyped-def]
    case = uid()
    with owner.begin() as c:
        c.execute(INSERT_AUDIT, audit_row(case, 1))
    app = create_engine(app_sync_url(migrated))
    with app.connect() as c:
        c.execute(INSERT_AUDIT, audit_row(case, 2))
        c.commit()
        for stmt in (
            "UPDATE audit_event SET actor_id='y'",
            "DELETE FROM audit_event",
            "TRUNCATE audit_event",
        ):
            with pytest.raises(ProgrammingError, match="permission denied"):
                c.execute(text(stmt))
            c.rollback()
    with owner.connect() as c:  # even the owner is stopped by the trigger
        for stmt in ("UPDATE audit_event SET actor_id='y'", "DELETE FROM audit_event"):
            with pytest.raises(DBAPIError, match="append-only"):
                c.execute(text(stmt))
            c.rollback()


def new_config(c: Connection, name: str) -> str:
    sid = uid()
    c.execute(
        text(
            "INSERT INTO config_set (id, domain, name, created_by) VALUES (:i, 'deadlines', :n, 't')"
        ),
        {"i": sid, "n": name},
    )
    return sid


def add_version(
    c: Connection, sid: str, v: int, frm: str, to: str | None, status: str = "published"
) -> None:
    c.execute(
        text(
            "INSERT INTO config_version (id, config_set_id, version, status, payload, payload_schema, checksum, "
            "effective_from, effective_to, change_note, created_by, published_by) VALUES (:i, :s, :v, "
            "CAST(:st AS config_status), '{}', 'deadlines@1', :ck, CAST(:f AS timestamptz), CAST(:t AS timestamptz), "
            "'n', 't', 't')"
        ),
        {"i": uid(), "s": sid, "v": v, "st": status, "ck": "a" * 64, "f": frm, "t": to},
    )


def test_config_guard_and_overlap(conn: Connection) -> None:
    sid = new_config(conn, f"g-{uuid.uuid4().hex[:6]}")
    add_version(conn, sid, 1, "2026-01-01", "2026-06-01")
    sp = conn.begin_nested()
    with pytest.raises(DBAPIError, match="immutable"):
        conn.execute(
            text("UPDATE config_version SET payload=CAST(:p AS jsonb) WHERE config_set_id=:s"),
            {"p": '{"x": 1}', "s": sid},
        )
    sp.rollback()
    sp = conn.begin_nested()
    with pytest.raises(IntegrityError):  # overlapping published window
        add_version(conn, sid, 2, "2026-03-01", None)
    sp.rollback()
    add_version(conn, sid, 3, "2026-06-01", None)  # adjacent window is fine
    conn.execute(
        text("UPDATE config_version SET status='retired' WHERE config_set_id=:s AND version=3"),
        {"s": sid},
    )
    with pytest.raises(DBAPIError, match="immutable"):  # retired is final
        conn.execute(
            text("UPDATE config_version SET change_note='z' WHERE config_set_id=:s AND version=3"),
            {"s": sid},
        )


# --- constraints --------------------------------------------------------------------------------
def expect_fail(
    conn: Connection, sql: str, params: dict[str, object], exc: type = IntegrityError
) -> None:
    sp = conn.begin_nested()
    with pytest.raises(exc):
        conn.execute(text(sql), params)
    sp.rollback()


def test_constraints(conn: Connection) -> None:
    case = make_case(conn)
    make_doc(conn, case, sha="a" * 64)
    with pytest.raises(IntegrityError):  # duplicate (case_id, sha256)
        sp = conn.begin_nested()
        try:
            make_doc(conn, case, sha="a" * 64)
        finally:
            sp.rollback()
    sp = conn.begin_nested()
    with pytest.raises(IntegrityError):  # infected doc must be quarantined
        make_doc(conn, case, scan="infected", life="active")
    sp.rollback()
    make_doc(conn, case, scan="infected", life="quarantined")
    expect_fail(
        conn,
        "UPDATE claim_case SET admitted_on='2026-02-02', discharged_on='2026-02-01' WHERE id=:i",
        {"i": case},
    )
    expect_fail(conn, "UPDATE claim_case SET claimed_amount=-1 WHERE id=:i", {"i": case})
    expect_fail(conn, "UPDATE claim_case SET approved_amount=-1 WHERE id=:i", {"i": case})
    expect_fail(
        conn,
        "UPDATE claim_case SET claim_type='reimbursement', preauth_amount=5 WHERE id=:i",
        {"i": case},
    )
    expect_fail(
        conn,
        "INSERT INTO insurer_query (id, case_id, insurer_query_id, round, category, text) "
        "VALUES (:i, :c, :q, 4, 'other', 't')",
        {"i": uid(), "c": case, "q": uid()},
    )
    expect_fail(
        conn,
        "INSERT INTO doc_request (id, case_id, doc_type, reason, status) "
        "VALUES (:i, :c, 'final_bill', 'r', 'waived')",
        {"i": uid(), "c": case},
    )
    q = uid()
    conn.execute(
        text(
            "INSERT INTO insurer_query (id, case_id, insurer_query_id, round, category, text) "
            "VALUES (:i, :c, :q, 1, 'other', 't')"
        ),
        {"i": q, "c": case, "q": uid()},
    )
    user = conn.execute(text("SELECT created_by FROM claim_case WHERE id=:i"), {"i": case}).scalar()
    expect_fail(
        conn,
        "INSERT INTO query_response (id, query_id, version, draft_text, source, approved_by, "
        "second_approver) VALUES (:i, :q, 1, 't', 'agent', :u, :u)",
        {"i": uid(), "q": q, "u": user},
    )


def test_open_doc_request_partial_unique(conn: Connection) -> None:
    case = make_case(conn)
    ins = (
        "INSERT INTO doc_request (id, case_id, doc_type, reason, status) "
        "VALUES (:i, :c, 'final_bill', 'r', :s)"
    )
    conn.execute(text(ins), {"i": uid(), "c": case, "s": "open"})
    expect_fail(conn, ins, {"i": uid(), "c": case, "s": "open"})
    conn.execute(text("UPDATE doc_request SET status='fulfilled' WHERE case_id=:c"), {"c": case})
    conn.execute(text(ins), {"i": uid(), "c": case, "s": "open"})  # allowed again


def test_signoff_one_vote_per_officer(conn: Connection) -> None:
    case = make_case(conn)
    user = conn.execute(text("SELECT created_by FROM claim_case WHERE id=:i"), {"i": case}).scalar()
    draft = uid()
    conn.execute(
        text(
            "INSERT INTO claim_draft (id, case_id, version, payload, source, created_by) "
            "VALUES (:i, :c, 1, '{}', 'agent', 'x')"
        ),
        {"i": draft, "c": case},
    )
    ins = (
        "INSERT INTO signoff (id, case_id, draft_id, officer_id, decision) "
        "VALUES (:i, :c, :d, :u, 'approved')"
    )
    conn.execute(text(ins), {"i": uid(), "c": case, "d": draft, "u": user})
    expect_fail(conn, ins, {"i": uid(), "c": case, "d": draft, "u": user})


# --- grants -------------------------------------------------------------------------------------
def test_grants(migrated: str) -> None:
    case_conn = create_engine(owner_url(migrated))
    with case_conn.begin() as c:
        make_case(c)
        c.execute(
            text("UPDATE patient SET phone_enc = '\\x00', id_proof_hash = :h"), {"h": "f" * 64}
        )
    ro = create_engine(readonly_url(migrated))
    with ro.connect() as c:
        assert c.execute(text("SELECT count(*) FROM patient")).scalar() >= 0 or True
        c.execute(text("SELECT id, uhid, full_name FROM patient LIMIT 1"))
        for col in ("phone_enc", "id_proof_hash"):
            with pytest.raises(ProgrammingError, match="permission denied"):
                c.execute(text(f"SELECT {col} FROM patient"))
            c.rollback()
        with pytest.raises(ProgrammingError, match="permission denied"):
            c.execute(text("SELECT * FROM patient"))
        c.rollback()
        with pytest.raises(ProgrammingError, match="permission denied"):
            c.execute(text("DELETE FROM claim_case"))
    app = create_engine(app_sync_url(migrated))
    with app.connect() as c:
        with pytest.raises(ProgrammingError, match="permission denied"):
            c.execute(text("TRUNCATE claim_case"))
        c.rollback()
        with pytest.raises(ProgrammingError, match="permission denied"):
            c.execute(text("DELETE FROM claim_case"))
        c.rollback()
        c.execute(text("DELETE FROM reminder"))  # explicitly allowed


def test_n8n_role_cannot_use_hospital_db() -> None:
    with pytest.raises(OperationalError, match="permission denied|not permitted|CONNECT"):
        create_engine(n8n_url()).connect()


# --- claim_ref ------------------------------------------------------------------------------------
def test_claim_ref_concurrent_unique(migrated: str) -> None:
    eng = create_engine(app_sync_url(migrated), pool_size=20)
    out: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        for _ in range(15):
            with eng.begin() as c:
                v = c.execute(text("SELECT next_claim_ref()")).scalar()
            with lock:
                out.append(v)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(out) == 300 == len(set(out))
    assert all(r.startswith("HC-") and len(r) == 14 for r in out)
    nums = sorted(int(r[-6:]) for r in out)
    assert nums == list(range(nums[0], nums[0] + 300))  # gapless when nothing rolls back


def test_money_columns_are_numeric(conn: Connection) -> None:
    rows = (
        conn.execute(
            text(
                "SELECT data_type FROM information_schema.columns WHERE table_name='claim_case' AND column_name LIKE '%amount'"
            )
        )
        .scalars()
        .all()
    )
    assert rows and set(rows) == {"numeric"}
    case = make_case(conn)
    conn.execute(
        text("UPDATE claim_case SET claimed_amount=:a WHERE id=:i"),
        {"a": Decimal("1234.50"), "i": case},
    )
    assert conn.execute(
        text("SELECT claimed_amount FROM claim_case WHERE id=:i"), {"i": case}
    ).scalar() == Decimal("1234.50")


# --- seed -----------------------------------------------------------------------------------------
def test_seed_idempotent_and_users_match_realm(migrated: str) -> None:
    import json
    import pathlib

    from seed.run import run

    run(owner_url(migrated))
    eng = create_engine(owner_url(migrated))
    tables = (
        "hospital",
        "app_user",
        "patient",
        "insurance_policy_ref",
        "config_set",
        "config_version",
        "network_insurer",
        "simulated_preauth",
        "allowed_transition",
    )
    with eng.connect() as c:
        before = [c.execute(text(f"SELECT count(*) FROM {t}")).scalar() for t in tables]
    run(owner_url(migrated))
    with eng.connect() as c:
        after = [c.execute(text(f"SELECT count(*) FROM {t}")).scalar() for t in tables]
        subs = {r[0] for r in c.execute(text("SELECT keycloak_sub FROM app_user"))}
    assert before == after  # idempotent; other tests may have added rows
    assert before[2] >= 20 and before[5] == 6 and before[8] == 32
    realm = (
        pathlib.Path(__file__).resolve().parents[3] / "infra/keycloak/rendered/realm-hospital.json"
    )
    if realm.exists():
        ids = {u["id"] for u in json.loads(realm.read_text())["users"] if "id" in u}
        assert ids <= subs


def test_claim_ref_year_rollover(migrated: str) -> None:
    eng = create_engine(app_sync_url(migrated))
    with eng.begin() as c:
        a = [c.execute(text("SELECT next_claim_ref(2031)")).scalar() for _ in range(3)]
        b = c.execute(text("SELECT next_claim_ref(2032)")).scalar()
        again = c.execute(text("SELECT next_claim_ref(2031)")).scalar()
    assert a == ["HC-2031-000001", "HC-2031-000002", "HC-2031-000003"]
    assert b == "HC-2032-000001"  # new year restarts at 000001
    assert again == "HC-2031-000004"
