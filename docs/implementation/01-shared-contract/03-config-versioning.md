# 01-03 — Configuration as Versioned Rows

Status: PROPOSED. Joint design; each side stores its own tables (no shared database, per interface rules).

## 1. Goal
Principle 2: document lists, deadlines, thresholds and policy rules are database rows, versioned and immutable once published. Every decision records the versions it used so any outcome can be reproduced and audited later. Admins change behaviour without redeploying.

## 2. Inputs / Outputs
- Input: admin edits (draft payloads), seed defaults, historical cases for dry-runs.
- Output: `resolve(domain, key, at)` results used by completeness, router, decision gate and calc mapper; `ConfigVersions` stamps on submissions and decisions; audit events `config.drafted|validated|published|retired`.

## 3. Data model — full DDL (identical shape on both sides)
```sql
CREATE TYPE config_status AS ENUM ('draft','published','retired');

CREATE TABLE config_set (
  id            UUID PRIMARY KEY,
  domain        TEXT NOT NULL,                -- e.g. doc_requirements
  name          TEXT NOT NULL,                -- scope key e.g. 'cashless/planned/cardiac' or 'default'
  description   TEXT,
  created_by    TEXT NOT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (domain, name)
);

CREATE TABLE config_version (
  id              UUID PRIMARY KEY,
  config_set_id   UUID NOT NULL REFERENCES config_set(id),
  version         INT  NOT NULL,
  status          config_status NOT NULL DEFAULT 'draft',
  payload         JSONB NOT NULL,
  payload_schema  TEXT NOT NULL,              -- schema id + version, e.g. doc_requirements@1
  checksum        CHAR(64) NOT NULL,          -- sha256 of canonical payload
  effective_from  TIMESTAMPTZ,
  effective_to    TIMESTAMPTZ,
  change_note     TEXT NOT NULL,
  created_by      TEXT NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  published_by    TEXT,
  second_approver TEXT,                       -- for two-person domains
  published_at    TIMESTAMPTZ,
  UNIQUE (config_set_id, version),
  CHECK (status <> 'published' OR (effective_from IS NOT NULL AND published_by IS NOT NULL)),
  CHECK (effective_to IS NULL OR effective_to > effective_from)
);
CREATE INDEX config_version_resolve ON config_version (config_set_id, status, effective_from);

-- no overlapping published windows per set
CREATE EXTENSION IF NOT EXISTS btree_gist;
ALTER TABLE config_version ADD CONSTRAINT no_overlap
  EXCLUDE USING gist (config_set_id WITH =, tstzrange(effective_from, effective_to) WITH &&)
  WHERE (status = 'published');

-- immutability
CREATE FUNCTION config_version_guard() RETURNS trigger AS $$
BEGIN
  IF OLD.status = 'published' AND (NEW.payload <> OLD.payload OR NEW.version <> OLD.version
     OR NEW.checksum <> OLD.checksum OR NEW.effective_from <> OLD.effective_from) THEN
     RAISE EXCEPTION 'published config_version is immutable';
  END IF;
  IF OLD.status = 'retired' AND NEW.status <> 'retired' THEN
     RAISE EXCEPTION 'retired config_version cannot be reopened';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
CREATE TRIGGER config_version_immutable BEFORE UPDATE ON config_version
  FOR EACH ROW EXECUTE FUNCTION config_version_guard();
CREATE TRIGGER config_version_nodelete BEFORE DELETE ON config_version
  FOR EACH ROW WHEN (OLD.status <> 'draft') EXECUTE FUNCTION config_version_guard();  -- raises
```
The only permitted update on a published row is `status → retired` and setting `effective_to`.

## 4. Domains and payload schemas

### 4.1 Hospital side
| Domain | Scope key (`name`) | Payload (JSON Schema `domain@1`) |
|---|---|---|
| `doc_requirements` | `{claim_type}/{admission_type}/{procedure_group\|default}` | see 4.1.1 |
| `deadlines` | `default` or per insurer | `reminder_offsets_hours: [24,72,168]`, `reimbursement_filing_days: 30`, `query_response_sla_hours: 72`, `intimation_emergency_hours: 24` |
| `router_rules` | `default` | rules list: `{when: {...}, then: {claim_type, flags[]}}` evaluated in order |
| `confidence_gates` | `default` | `parse_min: 0.75`, `classification_min: 0.80`, `agreement_min: 0.90` (two-pass), `quality_min: 0.5`, `amount_tolerance_inr: 1.00` (names match hospital docs 03/04) |

4.1.1 `doc_requirements` payload. User decision: the default list is **prescriptions and bills only** (medicine prescriptions with the corresponding medicine bills, procedure bills, hospital-charge bills), arranged chronologically; bills must carry a hospital stamp. Other document types (claim form, ID proof, policy card, discharge summary, FIR/MLC, cheque, receipts) are no longer required by default; they stay available as optional or conditional rules, so the list remains pure configuration:
```json
{
  "required": ["prescription","pharmacy_bill","final_bill"],
  "optional": ["itemised_bill","lab_report","discharge_summary"],
  "conditional": [
    {"if": {"bill_has_category": "surgery"}, "require": ["procedure_bill"]},
    {"if": {"bill_has_category": "implant"}, "require": ["implant_sticker"]}
  ],
  "stamp_required_doc_types": ["pharmacy_bill","procedure_bill","final_bill","itemised_bill"],
  "chronological": true,
  "min_parse_confidence": 0.80,
  "max_pages_per_doc": 300
}
```

### 4.2 Insurer side
| Domain | Scope key | Payload |
|---|---|---|
| `policy_rules` | `{product_code}` | see 4.2.1 |
| `thresholds` | `default` | `T_auto_inr: 50000`, `T_four_inr: 500000`, `identity_min_score: 0.90`, `authenticity_min_score: 0.80`, `dual_approval_distinct_roles: false` |
| `query_policy` | `default` | `max_rounds: 3`, `round_sla_hours: [72,48,24]` (used only for `due_by`; no 50%/80% elapsed reminders, overdue is marked at 100%), `escalation_role: "senior_reviewer"`, `auto_close_after_days: 30` |
| `doc_requirements` | same as hospital but insurer's list | |
| `exclusions` | `{product_code}` | code lists (ICD/procedure) with waiting-period months |

4.2.1 `policy_rules` payload:
```json
{
  "product_code": "NIV-FLOAT-5L",
  "sum_insured_inr": 500000,
  "room_rent_cap": {"type": "pct_of_si_per_day", "value": 1.0, "icu_value": 2.0},
  "sublimits": [ {"procedure_group": "cataract", "cap_inr": 40000, "per": "eye"} ],
  "co_pay_pct": 10, "co_pay_applies_to": ["all"],
  "deductible_inr": 0,
  "waiting_periods": {"initial_days": 30, "pre_existing_months": 24, "specific_months": 12},
  "proportionate_deduction": true,
  "network_discount_pct": 0,
  "order_of_application": ["exclusions","sublimits","room_rent_cap","proportionate_deduction","co_pay","deductible","sum_insured_cap"]
}
```
`order_of_application` is authoritative for calc-engine; see 03-dev-B/07.

PROPOSED defaults: `T_auto = ₹50,000`, `T_four = ₹5,00,000`. `T_auto` is the auto-approve ceiling (all hard gates must also pass); it is re-tuned on the synthetic evaluation set so the false auto-approve rate stays below ~1%. Above `T_auto` a human decides (one approver up to `T_four`, two distinct approvers above).

## 5. ConfigVersions stamps
Hospital → insurer (on `ClaimSubmission`):
```python
class ConfigVersions(BaseModel):
    doc_requirements: int
    deadlines: int
    router_rules: int
    confidence_gates: int
```
Insurer's decision record (stored, returned in `Decision.policy_version` and internal audit):
`policy_rules_v`, `thresholds_v`, `query_policy_v`, `doc_requirements_v`, `exclusions_v`, `calc_engine_version` (semver + git sha), `prompt_versions` (map agent→version from Langfuse).

A case row stores the versions it started with (`config_snapshot JSONB`); re-evaluation after publish creates a second snapshot and keeps both.

## 6. Resolution algorithm
```python
async def resolve(domain: str, name: str, at: datetime | None = None) -> ResolvedConfig:
    at = at or utcnow()
    key = (domain, name, at.replace(second=0, microsecond=0) if at_is_now else at)
    if hit := cache.get(key):
        return hit
    row = await db.fetchrow(
        """
      SELECT cv.* FROM config_version cv JOIN config_set cs ON cs.id = cv.config_set_id
      WHERE cs.domain=$1 AND cs.name=$2 AND cv.status IN ('published','retired')
        AND cv.effective_from <= $3 AND (cv.effective_to IS NULL OR cv.effective_to > $3)
      ORDER BY cv.effective_from DESC LIMIT 1""",
        domain,
        name,
        at,
    )
    if row is None and name != "default":
        return await resolve(domain, "default", at)  # fallback to default scope
    if row is None:
        raise ConfigNotFound(domain, name)
    payload = SCHEMAS[row.payload_schema].model_validate(row.payload)
    cfg = ResolvedConfig(version=row.version, payload=payload, checksum=row.checksum)
    cache.set(key, cfg, ttl=60)
    return cfg
```
Scope lookup for doc requirements: try `claim/adm/procedure_group`, then `claim/adm/default`, then `default`.
Cache: in-process TTL 60 s + invalidation on Redis pub/sub channel `config.changed` (message: `{domain, name, version}`); on message drop matching keys.

## 7. Admin workflow
1. **Draft**: `POST /admin/config/{domain}/{name}/versions` creates next version number (max+1) with `status=draft`; editable.
2. **Validate**: JSON Schema + semantic checks (e.g. conditional rules reference known doc types; thresholds `T_auto < T_four`; policy `order_of_application` is a permutation of known steps).
3. **Dry-run**: replay up to N=200 historical cases (or synthetic set) with the draft vs current published; produce a diff table (cases whose outcome/missing-docs/approval route changed). Stored as `config_dry_run(id, version_id, summary JSONB, created_at)`.
4. **Publish**: sets `effective_from` (default now, may be future), `published_by`; two-person rule for domains `thresholds`, `policy_rules`, `exclusions` (`second_approver` ≠ `published_by` ≠ `created_by` is NOT required but `second_approver ≠ published_by` is). Previous published version's `effective_to` set to the new `effective_from` in the same transaction.
5. **Retire**: sets status `retired`; in-flight cases keep their snapshot.
6. Every step → audit event (01-04) with payload checksum and diff summary.

Endpoints (served by each API under `/admin/config`, role Admin): `GET /domains`, `GET /{domain}`, `GET /{domain}/{name}/versions`, `POST …/versions`, `PUT …/versions/{v}` (draft only), `POST …/versions/{v}:validate`, `:dry-run`, `:publish`, `:retire`, `GET …/versions/{v}/diff?against=`.

## 8. Edge cases
| Case | Behaviour |
|---|---|
| Two admins edit same draft | optimistic lock via `If-Match: checksum`; loser gets 412 |
| Publish with future `effective_from` | resolution picks previous until then; cache TTL covers it |
| Overlapping windows | rejected by exclusion constraint |
| Config missing for scope and no default | `ConfigNotFound` → case goes to `needs_manual_config` flag; never silently default |
| Payload fails schema on read (schema evolved) | fail closed (error) and alert; schemas versioned `domain@N`, migrations convert on publish |
| Clock skew between app and DB | use DB `now()` for effective-time comparisons |
| In-flight case when thresholds change | keeps snapshot; officer may "re-evaluate" (new snapshot, audit `config.reevaluated`) |
| Rollback | publish a new version copying the old payload (never edit/delete) |

## 9. Build tasks
1. Alembic migration `0002_config.py` on each side with the DDL in section 3 (enum, tables, exclusion constraint, triggers, `btree_gist`).
2. `app/config/schemas.py` — Pydantic payload models per domain (section 4) with `schema_id = "domain@1"`; JSON Schema export to `contract/openapi/config/`.
3. `app/config/service.py` — `ConfigService.resolve()`, cache, pub/sub subscriber task started at app startup.
4. `app/config/validators.py` — semantic checks per domain.
5. `app/config/dryrun.py` — replay harness hook (each side supplies a function that evaluates a case under a given config).
6. `app/api/admin_config.py` — endpoints from section 7 with RBAC and audit.
7. `scripts/seed_config.py` — defaults for synthetic data (doc requirements for 3 procedure groups; one policy product; thresholds).
8. Tests (section 10).
9. Admin UI screens are specified in the UI docs (02-dev-A/10, 03-dev-B/10).

## 10. Test matrix
| # | Test | Expected |
|---|---|---|
| C1 | UPDATE payload of published row via raw SQL | exception from trigger |
| C2 | DELETE published row | exception |
| C3 | retire then re-publish same row | exception |
| C4 | publish overlapping window | exclusion constraint violation |
| C5 | resolve at timestamp before first version | `ConfigNotFound` |
| C6 | resolve at past timestamp after two versions | returns the then-current version |
| C7 | scope fallback `cashless/planned/cardiac` → default | falls back correctly |
| C8 | cache invalidation via pub/sub | new version seen < 1 s |
| C9 | publish thresholds without second approver | 422 `second_approver_required` |
| C10 | `T_auto ≥ T_four` validation | rejected |
| C11 | dry-run diff on 50 synthetic cases | summary counts correct |
| C12 | decision record contains all version fields | assertion over 20 synthetic decisions |
| C13 | concurrent publish of two drafts | one wins; the other gets 409 |
| C14 | `If-Match` stale checksum | 412 |

## 11. Acceptance criteria
Updating a published row fails at DB level; resolution at historical timestamps returns historical versions; every Decision and every `ClaimSubmission` carries all version fields; dry-run works on seed data; both Admin UIs can publish a threshold with two people.

## 12. Claude Code kickoff prompt
> Implement tasks 1-5 of docs/implementation/01-shared-contract/03-config-versioning.md for the side I own (I am Dev A | Dev B — I will say which), using only that side's domains from section 4. Include tests C1-C8 and C10. Admin endpoints (task 6) come in a later session.
