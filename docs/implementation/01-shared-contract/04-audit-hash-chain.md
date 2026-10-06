# 01-04 — Append-only Hash-chained Audit Log

Status: PROPOSED. Shared library at `contract/python/claim_contract/audit.py`; each side has its own tables (no shared DB).

## 1. Goal
Principle 6: every agent output, human edit and approval is an append-only, tamper-evident event. After the fact, anyone can verify that no event was altered, removed or reordered, and can see which versions of config, prompts and models produced a result.

## 2. Inputs / Outputs
- Input: events from APIs, n8n (via API), crews (returned objects persisted by API), human actions.
- Output: ordered per-case event stream, `verify_chain()` result, daily anchors, SSE notifications (`audit.appended`), read API.

## 3. Data model — full DDL
```sql
CREATE TYPE audit_actor_type AS ENUM ('agent','human','system','external');

CREATE TABLE audit_event (
  id              UUID PRIMARY KEY,
  case_id         UUID NOT NULL,
  seq             BIGINT NOT NULL,
  ts              TIMESTAMPTZ NOT NULL,
  actor_type      audit_actor_type NOT NULL,
  actor_id        TEXT NOT NULL,                 -- user id, agent name@version, service name
  event_type      TEXT NOT NULL,
  payload         JSONB NOT NULL,                -- redacted
  config_versions JSONB NOT NULL DEFAULT '{}',
  model_info      JSONB,                         -- {alias, provider, model, prompt_version, trace_id, temperature, tokens_in, tokens_out}
  journey_id      UUID,                          -- v1.1
  prev_hash       CHAR(64) NOT NULL,
  hash            CHAR(64) NOT NULL,
  UNIQUE (case_id, seq)
);
CREATE INDEX audit_event_case ON audit_event (case_id, seq);
CREATE INDEX audit_event_type_ts ON audit_event (event_type, ts);

CREATE TABLE case_audit_head (
  case_id   UUID PRIMARY KEY,
  last_seq  BIGINT NOT NULL,
  last_hash CHAR(64) NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE audit_anchor (
  anchor_date  DATE PRIMARY KEY,
  merkle_root  CHAR(64) NOT NULL,
  case_count   INT NOT NULL,
  object_key   TEXT,                -- MinIO object-locked copy
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- append-only enforcement
CREATE FUNCTION audit_event_block() RETURNS trigger AS $$
BEGIN RAISE EXCEPTION 'audit_event is append-only'; END $$ LANGUAGE plpgsql;
CREATE TRIGGER audit_event_no_update BEFORE UPDATE OR DELETE ON audit_event
  FOR EACH ROW EXECUTE FUNCTION audit_event_block();
CREATE TRIGGER audit_event_no_truncate BEFORE TRUNCATE ON audit_event
  FOR EACH STATEMENT EXECUTE FUNCTION audit_event_block();

REVOKE UPDATE, DELETE, TRUNCATE ON audit_event FROM app_role;
GRANT INSERT, SELECT ON audit_event TO app_role;
GRANT SELECT, INSERT, UPDATE ON case_audit_head TO app_role;
```

## 4. Hash computation
```
hash = SHA256( prev_hash_hex_ascii || canonical_json(body) )   -- hex output
body = {seq, case_id, ts, actor_type, actor_id, event_type, payload, config_versions, model_info}
```
Canonical JSON: keys sorted lexicographically at every level, separators `(",",":")`, UTF-8 without ASCII escaping, decimals as strings, timestamps `YYYY-MM-DDTHH:MM:SSZ` (seconds precision; sub-second stored in `payload` if needed), `null` kept. Genesis `prev_hash = "0" * 64`. `journey_id` is stored but **excluded** from the hash body so v1.0 and v1.1 events hash identically.

### 4.1 Test vectors (computed)
Event 1 (`seq=1`, `case_id=0199a1b2-0000-7000-8000-000000000001`, `ts=2026-10-06T10:00:00Z`, `actor_type=system`, `actor_id=hospital-api`, `event_type=case.created`, `payload={"claim_type":"cashless"}`, `config_versions={"doc_requirements":1}`, `model_info=null`):

Canonical body:
```
{"actor_id":"hospital-api","actor_type":"system","case_id":"0199a1b2-0000-7000-8000-000000000001","config_versions":{"doc_requirements":1},"event_type":"case.created","model_info":null,"payload":{"claim_type":"cashless"},"seq":1,"ts":"2026-10-06T10:00:00Z"}
```
`hash1 = 271ed01f81f1b1664abd152648469ac3d00c460ad241b0582e9da38510586165` (prev = 64 zeros)

Event 2: same case, `seq=2`, `ts=2026-10-06T10:01:00Z`, `event_type=doc.uploaded`, `payload={"doc_id":"d1","sha256":"abababababababababababababababababababababababababababababababab"}`, other fields as event 1.
`hash2 = faad23c74acb36c3d5c543e760ff855cb950c1c3a5c3683e7f3956ddba13b463`

Both must be reproduced by the library unit tests.

## 5. Append procedure (single transaction)
```python
async def append(
    db,
    case_id,
    actor_type,
    actor_id,
    event_type,
    payload,
    config_versions=None,
    model_info=None,
    journey_id=None,
):
    payload = redact(payload)  # section 7
    assert_event_type_known(event_type)  # registry in section 8
    async with db.begin():
        head = await db.fetchrow(
            "INSERT INTO case_audit_head(case_id,last_seq,last_hash) VALUES($1,0,$2) "
            "ON CONFLICT (case_id) DO UPDATE SET case_id=EXCLUDED.case_id RETURNING *",  # ensure row
            case_id,
            "0" * 64,
        )
        head = await db.fetchrow(
            "SELECT * FROM case_audit_head WHERE case_id=$1 FOR UPDATE", case_id
        )
        seq = head.last_seq + 1
        ts = utcnow().replace(microsecond=0)
        body = {...}
        h = sha256_hex(head.last_hash + canonical_json(body))
        await db.execute("INSERT INTO audit_event(...) VALUES(...)", ...)
        await db.execute(
            "UPDATE case_audit_head SET last_seq=$2,last_hash=$3,updated_at=now() WHERE case_id=$1",
            case_id,
            seq,
            h,
        )
    await redis.publish(
        "audit.appended",
        json.dumps({"case_id": str(case_id), "seq": seq, "event_type": event_type}),
    )
    return seq
```
Rules: the `FOR UPDATE` serialises concurrent appends to one case; different cases do not block each other. Appends that are part of a business transaction (status change) must use the **same** DB transaction (`append(tx, ...)` accepts an existing connection) so state and audit commit atomically.

## 6. Verification
```python
async def verify_chain(db, case_id) -> VerifyResult:
    prev = "0" * 64
    expected_seq = 1
    async for e in db.stream("SELECT * FROM audit_event WHERE case_id=$1 ORDER BY seq", case_id):
        if e.seq != expected_seq:
            return Broken(e.seq, "gap_or_reorder")
        if e.prev_hash != prev:
            return Broken(e.seq, "prev_hash_mismatch")
        if sha256_hex(prev + canonical_json(body_of(e))) != e.hash:
            return Broken(e.seq, "hash_mismatch")
        prev = e.hash
        expected_seq += 1
    head = await db.fetchrow("SELECT * FROM case_audit_head WHERE case_id=$1", case_id)
    if head and (head.last_seq != expected_seq - 1 or head.last_hash != prev):
        return Broken(None, "head_mismatch")  # truncation
    return Ok(expected_seq - 1)
```
- Nightly job (`scripts/audit_verify_all.py`): verify every case touched in the last 24 h plus a random 5% sample of older cases; on failure raise alert `audit_chain_broken` (see 05-integration/04).
- **Anchor**: after verification compute the Merkle root over `(case_id, last_hash)` pairs sorted by case_id; write `audit_anchor` and a JSON object to MinIO bucket with object lock (compliance mode, 1 year). A later rewrite of a whole chain is then detectable against the anchor.

## 7. Redaction (`redact()`)
Applied to every payload before hashing. Rules:
| Pattern | Action |
|---|---|
| Aadhaar `\b\d{4}\s?\d{4}\s?\d{4}\b` | replace with `[AADHAAR]` |
| PAN `\b[A-Z]{5}\d{4}[A-Z]\b` | `[PAN]` |
| Phone `\b(\+91[\s-]?)?[6-9]\d{9}\b` | `[PHONE]` |
| Email | `[EMAIL]` |
| Keys named `raw_id`, `id_number`, `address`, `full_text`, `ocr_text` | removed; replaced by `{name}_sha256` and length |
| Strings > 2000 chars | truncated with `…[truncated n]` plus sha256 of full |
| Patient names | replaced with `patient:<hash8>` in agent prompts/outputs; audit stores hashed form (human UI can resolve via case record) |
Allowed: hashes, doc ids, amounts, codes, enum values, scores, rule ids.
Test corpus `contract/tests/fixtures/pii_corpus.json` (50 strings with expected redactions); fuzz test ensures no regex hit remains after redaction.

## 8. Event-type registry (`audit_events.yaml`)
Event types are dotted `domain.entity.verb`; payload shape documented per type and validated by a registry of Pydantic models (unknown type → error).

| event_type | actor | payload essentials |
|---|---|---|
| `case.created` | system/human | claim_type, admission_type |
| `doc.uploaded` | human | doc_id, sha256, size, filename_hash |
| `doc.scanned` | system | doc_id, result (clean/infected) |
| `doc.parsed` | agent/system | doc_id, parser, confidence, pages |
| `doc.classified` | agent | doc_id, doc_type, passes[], agreement |
| `completeness.evaluated` | system | missing[], present[], blockers, config_versions |
| `claim.built` | agent | bill_line_count, totals, checks |
| `human.signoff` | human | role, comment_hash |
| `claim.submitted` | system | claim_ref, idempotency_key, contract_version |
| `ack.received` | external | insurer_claim_no, sequence |
| `query.received` / `query.draft_generated` / `query.edited` / `query.sent` | external/agent/human | query_id, round, category, diff_stats |
| `verification.step.completed` | agent | step (identity, authenticity, coverage, calc), score, findings[] |
| `decision.recommended` | agent | outcome, amount, calc_trace_id |
| `decision.auto_approved` | system | outcome, amount, calc_trace_id, gates (identity, authenticity, completeness, calc, flags), `T_auto`, `thresholds_v` |
| `human.approved` / `human.rejected` | human | decision_id, role, reason_codes, second_approver |
| `escalation.raised` | system | reason, round |
| `settlement.recorded` | system | settlement_id, amount, utr_hash |
| `config.published` / `config.retired` | human | domain, name, version, checksum |
| `outbox.dead` | system | endpoint, attempts |
| `audit.verify.failed` | system | case_id, seq, reason |

## 9. Read API (each side)
`GET /audit/{case_id}?after_seq=&limit=` → `{items:[…], verified: true|false|null, head:{seq,hash}}`; `GET /audit/{case_id}/verify` runs `verify_chain`. Role gates: Reviewer/Officer read own cases; Admin/Auditor read all. Export: `GET /audit/{case_id}/export` → NDJSON + verification report PDF (later).

## 10. Edge cases
| Case | Behaviour |
|---|---|
| Two appends race | `FOR UPDATE` serialises; seq gapless |
| Business tx rolls back | audit insert rolls back too (same tx) |
| Append fails after commit of business state | prevented by same-tx rule; lint rule flags `append` outside tx in handlers that mutate status |
| Clock skew | `ts` taken from the API host; not used for ordering — `seq` is |
| Huge payload | truncated per redaction rules; full artefacts live in MinIO referenced by sha256 |
| Event type misspelt | registry rejects |
| Crew returns object but API validation fails | API logs `validation.failed` event with error summary, not the raw output |
| DB superuser edits row | verification detects; anchor protects against full rewrite |
| Migration needing to alter history | forbidden; add a compensating event |

## 11. Build tasks
1. `contract/python/claim_contract/audit.py` — `canonical_json`, `compute_hash`, `redact`, `append`, `verify_chain`, `merkle_root`.
2. `contract/python/claim_contract/audit_events.py` — registry and payload models from section 8.
3. Alembic migration `0003_audit.py` (each side) with DDL from section 3 and role grants.
4. Unit tests: vectors from 4.1; canonical JSON edge cases (unicode, decimals, nested ordering).
5. Concurrency test: 50 coroutines appending to one case → seq 1..50 gapless, chain verifies.
6. Tamper tests: modify payload via superuser, delete a middle row, truncate tail, reorder seq → each detected with correct reason.
7. `app/api/audit.py` endpoints (section 9).
8. `scripts/audit_verify_all.py` and `scripts/audit_anchor.py` (cron via n8n schedule or container cron).
9. Redaction corpus + fuzz test.
10. Hook SSE: subscribe to `audit.appended` and forward to UI streams (see UI docs).

## 12. Test matrix
| # | Test | Expected |
|---|---|---|
| A1 | vectors 4.1 | exact hashes |
| A2 | app role UPDATE/DELETE/TRUNCATE | permission denied |
| A3 | superuser UPDATE | trigger exception (unless trigger disabled → verification catches) |
| A4 | 50 concurrent appends | seq 1..50, verify OK |
| A5 | delete middle event | `gap_or_reorder` |
| A6 | modify payload | `hash_mismatch` |
| A7 | truncate last 2 events | `head_mismatch` |
| A8 | rollback of business tx | no audit row |
| A9 | redaction corpus | zero PII regex matches |
| A10 | unknown event type | rejected |
| A11 | anchor recompute | Merkle root equals stored |
| A12 | journey_id differs | hash unchanged |

## 13. Acceptance criteria
All tests A1-A12 pass; app DB role cannot alter events; nightly verifier reports OK on seeded data and alerts on injected tampering; every event type in the registry is emitted at least once in the E2E scenario S01.

## 14. Dependencies
`01-02` (ids, enums), `01-03` (config version stamps), MinIO (04-shared-services/01) for anchors, Redis for pub/sub, Langfuse trace ids (04-shared-services/04) in `model_info`.

## 15. Claude Code kickoff prompt
> Implement docs/implementation/01-shared-contract/04-audit-hash-chain.md tasks 1-6 and 9 in `contract/` and the migration for the side I own. Reproduce vectors in 4.1 exactly. Concurrency and tamper tests are mandatory.
