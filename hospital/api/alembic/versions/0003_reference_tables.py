"""reference_tables"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

UP_SQL = r"""
CREATE TABLE app_user (
  id uuid PRIMARY KEY,
  keycloak_sub text NOT NULL,
  email citext NOT NULL,
  display_name text NOT NULL,
  role text NOT NULL,
  active boolean NOT NULL DEFAULT true,
  last_login_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_app_user_keycloak_sub UNIQUE (keycloak_sub),
  CONSTRAINT ck_app_user_role CHECK (role IN ('desk','officer','admin'))
);
CREATE INDEX ix_app_user_email ON app_user(email);

CREATE TABLE hospital (            -- one row in practice; supports multi-branch later
  id uuid PRIMARY KEY,
  code text NOT NULL,
  name text NOT NULL,
  nabh_accredited boolean NOT NULL DEFAULT false,
  rohini_id text,
  address jsonb,
  hmac_key_id text NOT NULL,       -- X-Key-Id used when signing outbound calls (01-01 §3)
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_hospital_code UNIQUE (code)
);

CREATE TABLE patient (
  id uuid PRIMARY KEY,
  uhid text NOT NULL,                       -- hospital MRN
  full_name text NOT NULL,
  dob date NOT NULL,
  gender char(1) NOT NULL,
  phone_enc bytea,                          -- AES-GCM, key HOSP_FIELD_KEY
  id_proof_type text,                       -- 'aadhaar'|'pan'|'passport'|'voter'|'dl' (label only)
  id_proof_hash char(64),                   -- SHA-256(pepper || raw); raw NEVER stored
  id_proof_last4 char(4),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_patient_uhid UNIQUE (uhid),
  CONSTRAINT ck_patient_gender CHECK (gender IN ('M','F','O')),
  CONSTRAINT ck_patient_dob CHECK (dob <= CURRENT_DATE)
);
CREATE INDEX ix_patient_name_trgm ON patient USING gin (full_name gin_trgm_ops);
CREATE INDEX ix_patient_uhid_trgm ON patient USING gin (uhid gin_trgm_ops);

CREATE TABLE insurance_policy_ref (  -- what the patient declares; truth lives at insurer
  id uuid PRIMARY KEY,
  patient_id uuid NOT NULL REFERENCES patient(id),
  insurer_name text NOT NULL,
  policy_number text NOT NULL,
  member_id text NOT NULL,
  valid_from date,
  valid_to date,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_policy_ref UNIQUE (patient_id, policy_number, member_id),
  CONSTRAINT ck_policy_ref_validity CHECK (valid_to IS NULL OR valid_from IS NULL OR valid_to >= valid_from)
);
CREATE INDEX ix_policy_ref_patient ON insurance_policy_ref(patient_id);

CREATE TRIGGER trg_app_user_updated BEFORE UPDATE ON app_user
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_hospital_updated BEFORE UPDATE ON hospital
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_patient_updated BEFORE UPDATE ON patient
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- hosp_readonly must not see phone_enc / id_proof_hash. A column-level REVOKE has no effect on a
-- table-level grant, so the table grant is removed and replaced by an explicit column list.
REVOKE SELECT ON patient FROM hosp_readonly;
GRANT SELECT (id, uhid, full_name, dob, gender, id_proof_type, id_proof_last4, created_at, updated_at)
  ON patient TO hosp_readonly;
"""

DOWN_SQL = r"""
DROP TABLE IF EXISTS insurance_policy_ref CASCADE;
DROP TABLE IF EXISTS patient CASCADE;
DROP TABLE IF EXISTS hospital CASCADE;
DROP TABLE IF EXISTS app_user CASCADE;
"""


def _run(sql: str) -> None:
    # raw cursor, no params: '%' in function bodies must not be read as a placeholder
    cur = op.get_bind().connection.cursor()
    cur.execute(sql)
    cur.close()


def upgrade() -> None:
    _run(UP_SQL)


def downgrade() -> None:
    _run(DOWN_SQL)
