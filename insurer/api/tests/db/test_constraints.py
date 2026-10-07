import uuid

import psycopg2
import pytest
from psycopg2 import errors as pe

pytestmark = pytest.mark.integration


@pytest.fixture
def cur(migrated_db):
    conn = psycopg2.connect(migrated_db)
    conn.autocommit = True
    c = conn.cursor()
    yield c
    conn.close()


def uid() -> str:
    return str(uuid.uuid4())


def make_product_policy(cur, sum_insured="500000", start="2026-01-01", end="2026-12-31"):
    pid, pol = uid(), uid()
    cur.execute("INSERT INTO core.insurance_product (id, code, name, insurer_name) VALUES (%s, %s, 'n', 'i')", (pid, f"P{pol[:6]}"))
    cur.execute(
        "INSERT INTO core.policy (id, policy_number, product_id, policy_holder_name, start_date, end_date, sum_insured, status) "
        "VALUES (%s, %s, %s, 'h', %s, %s, %s, 'active')", (pol, f"POL-{pol[:8]}", pid, start, end, sum_insured))
    return pol


def make_case(cur):
    hid, cid = uid(), uid()
    cur.execute("INSERT INTO core.network_hospital (id, hospital_code, name, network_status, hmac_key_id) VALUES (%s, %s, 'H', 'network', %s)",
                (hid, f"HOSP-{hid[:4]}", f"k-{hid[:6]}"))
    cur.execute(
        "INSERT INTO core.claim_case (id, insurer_claim_no, hospital_claim_ref, hospital_id, claim_type, admission_type, claimed_amount, "
        "contract_version, submission, submission_hash, received_at) VALUES (%s, %s, %s, %s, 'cashless', 'planned', 100, '1.0', '{}', %s, now())",
        (cid, f"IC-{cid[:8]}", f"HC-{cid[:6]}", hid, "0" * 64))
    return cid


def test_policy_constraints(cur):
    with pytest.raises(pe.CheckViolation):
        make_product_policy(cur, sum_insured="0")
    with pytest.raises(pe.CheckViolation):
        make_product_policy(cur, start="2026-05-01", end="2026-05-01")
    make_product_policy(cur)


def test_bad_status_enum_and_query_round(cur):
    cid = make_case(cur)
    with pytest.raises(pe.InvalidTextRepresentation):
        cur.execute("UPDATE core.claim_case SET status = 'bogus' WHERE id = %s", (cid,))
    with pytest.raises(pe.CheckViolation):
        cur.execute(
            "INSERT INTO core.query (id, case_id, round, category, text, status, origin, due_by) VALUES (%s, %s, 4, 'other', 't', 'open', 'human', now())",
            (uid(), cid))


def test_duplicate_approval_vote(cur):
    cid = make_case(cur)
    did = uid()
    cur.execute("INSERT INTO core.decision (id, case_id, kind, outcome, config_versions, created_by) VALUES (%s, %s, 'final', 'approve', '{}', 'u')", (did, cid))
    cur.execute("INSERT INTO core.approval (id, decision_id, approver, verdict, approver_role) VALUES (%s, %s, 'a1', 'approve', 'approver')", (uid(), did))
    with pytest.raises(pe.UniqueViolation):
        cur.execute("INSERT INTO core.approval (id, decision_id, approver, verdict, approver_role) VALUES (%s, %s, 'a1', 'approve', 'approver')", (uid(), did))


def _cfg_set(cur, domain="thresholds", name="default"):
    sid = uid()
    cur.execute("INSERT INTO config.config_set (id, domain, name, created_by) VALUES (%s, %s, %s, 'admin') ON CONFLICT DO NOTHING", (sid, domain, name))
    cur.execute("SELECT id FROM config.config_set WHERE domain=%s AND name=%s", (domain, name))
    return cur.fetchone()[0]


def _ver(cur, sid, v, frm, to=None, status="published"):
    vid = uid()
    cur.execute(
        "INSERT INTO config.config_version (id, config_set_id, version, status, payload, payload_schema, checksum, effective_from, effective_to, "
        "change_note, created_by, published_by) VALUES (%s,%s,%s,%s,'{\"a\":1}','t@1',%s,%s,%s,'n','a','b')",
        (vid, sid, v, status, "0" * 64, frm, to))
    return vid


def test_config_immutability_overlap_and_lifecycle(cur):
    sid = _cfg_set(cur, "thresholds", f"n-{uid()[:6]}")
    v1 = _ver(cur, sid, 1, "2026-01-01", "2026-06-01")
    with pytest.raises(pe.ExclusionViolation):
        _ver(cur, sid, 2, "2026-03-01")  # overlaps v1 window
    v2 = _ver(cur, sid, 2, "2026-06-01")
    with pytest.raises(Exception, match="immutable"):
        cur.execute("UPDATE config.config_version SET payload = '{\"a\":2}' WHERE id = %s", (v1,))
    with pytest.raises(Exception, match="cannot be deleted"):
        cur.execute("DELETE FROM config.config_version WHERE id = %s", (v1,))
    cur.execute("UPDATE config.config_version SET status = 'retired', effective_to = '2026-09-01' WHERE id = %s", (v2,))  # allowed
    with pytest.raises(Exception, match="cannot be reopened"):
        cur.execute("UPDATE config.config_version SET status = 'published' WHERE id = %s", (v2,))


def test_audit_immutable_for_owner(cur):
    cid = uid()
    cur.execute("INSERT INTO audit.case_audit_head (case_id, last_seq, last_hash) VALUES (%s, 1, %s)", (cid, "a" * 64))
    cur.execute(
        "INSERT INTO audit.audit_event (id, case_id, seq, ts, actor_type, actor_id, event_type, payload, prev_hash, hash) "
        "VALUES (%s, %s, 1, now(), 'system', 's', 'case.created', '{}', %s, %s)", (uid(), cid, "0" * 64, "a" * 64))
    with pytest.raises(Exception, match="append-only"):
        cur.execute("UPDATE audit.audit_event SET actor_id = 'x'")


def test_etag_bumps_and_claim_number_sequence(cur):
    cid = make_case(cur)
    cur.execute("SELECT etag FROM core.claim_case WHERE id = %s", (cid,))
    e0 = cur.fetchone()[0]
    cur.execute("UPDATE core.claim_case SET priority = 1 WHERE id = %s", (cid,))
    cur.execute("SELECT etag FROM core.claim_case WHERE id = %s", (cid,))
    assert cur.fetchone()[0] == e0 + 1
    cur.execute("SELECT nextval('ops.claim_no_seq'), nextval('ops.claim_no_seq')")
    a, b = cur.fetchone()
    assert b == a + 1


def test_roles_and_grants(migrated_db):
    base, _, db = migrated_db.rpartition("/")
    ro = psycopg2.connect(base.replace("postgres:testpw", "ins_readonly:ropw") + "/" + db)
    ro.autocommit = True
    rc = ro.cursor()
    rc.execute("SELECT count(*) FROM core.policy")
    with pytest.raises(pe.InsufficientPrivilege):
        rc.execute("DELETE FROM core.policy")
    with pytest.raises(pe.InsufficientPrivilege):
        rc.execute("INSERT INTO core.insurance_product (id, code, name, insurer_name) VALUES (gen_random_uuid(), 'x', 'x', 'x')")
    app = psycopg2.connect(base.replace("postgres:testpw", "ins_app:apppw") + "/" + db)
    app.autocommit = True
    ac = app.cursor()
    ac.execute("INSERT INTO core.insurance_product (id, code, name, insurer_name) VALUES (gen_random_uuid(), 'zz', 'x', 'x')")
    for stmt in ("DELETE FROM core.policy", "UPDATE audit.audit_event SET actor_id='x'", "DELETE FROM audit.audit_event",
                 "TRUNCATE audit.audit_event", "DROP TABLE core.policy"):
        with pytest.raises((pe.InsufficientPrivilege, pe.UndefinedTable)):
            ac.execute(stmt)
    ac.execute("DELETE FROM ops.outbox")  # allowed
    ro.close()
    app.close()
