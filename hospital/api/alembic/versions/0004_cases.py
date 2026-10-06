"""cases"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

UP_SQL = r"""
CREATE TABLE claim_case (
  id uuid PRIMARY KEY,
  claim_ref text NOT NULL,                              -- HC-YYYY-NNNNNN
  hospital_id uuid NOT NULL REFERENCES hospital(id),
  patient_id uuid NOT NULL REFERENCES patient(id),
  policy_ref_id uuid NOT NULL REFERENCES insurance_policy_ref(id),
  claim_type claim_type NOT NULL,
  admission_type admission_type NOT NULL,
  status hospital_case_status NOT NULL DEFAULT 'draft',
  admitted_on date,
  discharged_on date,
  diagnosis_codes text[] NOT NULL DEFAULT '{}',
  procedure_codes text[] NOT NULL DEFAULT '{}',
  procedure_group text,                                 -- derived by router (doc 05)
  treating_doctor text,
  preauth_ref text,
  preauth_amount numeric(14,2),
  claimed_amount numeric(14,2),
  insurer_claim_no text,                                -- set from Acknowledgement
  filing_deadline date,                                 -- reimbursement: discharged_on + deadlines.reimbursement_filing_days
  assigned_to uuid REFERENCES app_user(id),
  created_by uuid NOT NULL REFERENCES app_user(id),
  config_versions jsonb,                                -- ConfigVersions snapshot at creation (01-03 §4)
  stale_flagged_at timestamptz,                         -- doc 03 task 15: abandoned draft > 30 d
  version int NOT NULL DEFAULT 1,                       -- optimistic lock
  closed_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_claim_case_claim_ref UNIQUE (claim_ref),
  CONSTRAINT ck_case_dates CHECK (discharged_on IS NULL OR admitted_on IS NULL OR discharged_on >= admitted_on),
  CONSTRAINT ck_case_amounts CHECK (
        (claimed_amount IS NULL OR claimed_amount >= 0)
    AND (preauth_amount IS NULL OR preauth_amount >= 0)),
  CONSTRAINT ck_case_cashless_preauth_amount CHECK (preauth_amount IS NULL OR claim_type = 'cashless')
);
CREATE INDEX ix_case_status ON claim_case(status);
CREATE INDEX ix_case_patient ON claim_case(patient_id);
CREATE INDEX ix_case_assigned ON claim_case(assigned_to) WHERE assigned_to IS NOT NULL;
CREATE INDEX ix_case_created_keyset ON claim_case(created_at DESC, id DESC);
CREATE INDEX ix_case_deadline ON claim_case(filing_deadline) WHERE status NOT IN ('closed','settled');
CREATE INDEX ix_case_insurer_claim_no ON claim_case(insurer_claim_no) WHERE insurer_claim_no IS NOT NULL;
CREATE INDEX ix_case_claim_ref_trgm ON claim_case USING gin (claim_ref gin_trgm_ops);

CREATE TABLE case_status_history (
  id uuid PRIMARY KEY,
  case_id uuid NOT NULL REFERENCES claim_case(id),
  from_status hospital_case_status,
  to_status hospital_case_status NOT NULL,
  actor_id text NOT NULL,                       -- user uuid, 'svc-n8n', 'insurer-callback', 'system'
  reason text,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_case_status_history_case ON case_status_history(case_id, created_at);

CREATE TABLE allowed_transition (               -- seeded from contract TRANSITIONS
  from_status hospital_case_status NOT NULL,
  to_status hospital_case_status NOT NULL,
  PRIMARY KEY (from_status, to_status)
);



CREATE TRIGGER trg_claim_case_updated BEFORE UPDATE ON claim_case
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE FUNCTION check_transition() RETURNS trigger AS $$
BEGIN
  IF NEW.status IS DISTINCT FROM OLD.status AND NOT EXISTS (
       SELECT 1 FROM allowed_transition WHERE from_status = OLD.status AND to_status = NEW.status)
  THEN RAISE EXCEPTION 'invalid_transition % -> %', OLD.status, NEW.status USING ERRCODE = 'P0001';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
CREATE TRIGGER trg_case_transition BEFORE UPDATE OF status ON claim_case
  FOR EACH ROW EXECUTE FUNCTION check_transition();

CREATE FUNCTION next_claim_ref() RETURNS text AS $$
DECLARE y int := extract(year FROM now() AT TIME ZONE 'UTC'); s text := 'claim_ref_seq_' || y; n bigint;
BEGIN
  IF to_regclass(s) IS NULL THEN  -- lazy per-year sequence; the lock stops concurrent first callers racing in pg_class
    PERFORM pg_advisory_xact_lock(hashtext('next_claim_ref'));
    EXECUTE format('CREATE SEQUENCE IF NOT EXISTS %I', s);
  END IF;
  -- NOTE: without the lock, concurrent first calls fail with a pg_class unique violation (found by test).
  EXECUTE format('SELECT nextval(%L)', s) INTO n;
  RETURN 'HC-' || y || '-' || lpad(n::text, 6, '0');
END $$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = public;
REVOKE ALL ON FUNCTION next_claim_ref() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION next_claim_ref() TO hosp_app;

CREATE TRIGGER trg_claim_case_version BEFORE UPDATE ON claim_case
  FOR EACH ROW EXECUTE FUNCTION forbid_version_regress();
"""

DOWN_SQL = r"""
DROP TABLE IF EXISTS allowed_transition CASCADE;
DROP TABLE IF EXISTS case_status_history CASCADE;
DROP TABLE IF EXISTS claim_case CASCADE;
DROP FUNCTION IF EXISTS next_claim_ref();
DROP FUNCTION IF EXISTS check_transition();
"""


def _run(sql: str) -> None:
    # raw cursor, no params: '%' in function bodies must not be read as a placeholder
    cur = op.get_bind().connection.cursor()
    cur.execute(sql)
    cur.close()


def upgrade() -> None:
    _run(UP_SQL)
    from claim_contract.transitions import HOSPITAL_TRANSITIONS

    rows = [
        (a.value, b.value) for a, targets in HOSPITAL_TRANSITIONS.items() for b in sorted(targets)
    ]
    op.get_bind().exec_driver_sql(
        "INSERT INTO allowed_transition (from_status, to_status) "
        "VALUES (%s::hospital_case_status, %s::hospital_case_status)",
        rows,
    )


def downgrade() -> None:
    _run(DOWN_SQL)
