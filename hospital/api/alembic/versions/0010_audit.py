"""audit"""

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

UP_SQL = r"""
CREATE TYPE audit_actor_type AS ENUM ('agent','human','system','external');

CREATE TABLE audit_event (
  id uuid PRIMARY KEY,
  seq bigint NOT NULL,
  case_id uuid NOT NULL,                       -- no FK: audit outlives any row-level cleanup
  ts timestamptz NOT NULL,
  actor_type audit_actor_type NOT NULL,
  actor_id text NOT NULL,
  event_type text NOT NULL,
  payload jsonb NOT NULL,                      -- redacted (01-04 §7)
  config_versions jsonb NOT NULL DEFAULT '{}',
  model_info jsonb,
  journey_id uuid,                             -- contract v1.1; excluded from hash body
  prev_hash char(64) NOT NULL,
  hash char(64) NOT NULL,
  CONSTRAINT uq_audit_case_seq UNIQUE (case_id, seq)
);
CREATE INDEX ix_audit_case_ts ON audit_event(case_id, seq);
CREATE INDEX ix_audit_type ON audit_event(event_type, ts);

CREATE TABLE case_audit_head (
  case_id uuid PRIMARY KEY,
  last_seq bigint NOT NULL,
  last_hash char(64) NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE audit_anchor (
  anchor_date date PRIMARY KEY,
  merkle_root char(64) NOT NULL,
  case_count int NOT NULL,
  object_key text,                             -- MinIO object-locked copy
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TRIGGER trg_audit_immutable BEFORE UPDATE OR DELETE ON audit_event
  FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER trg_audit_no_truncate BEFORE TRUNCATE ON audit_event
  FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();
REVOKE UPDATE, DELETE, TRUNCATE ON audit_event FROM hosp_app;
"""

DOWN_SQL = r"""
DROP TABLE IF EXISTS audit_anchor CASCADE;
DROP TABLE IF EXISTS case_audit_head CASCADE;
DROP TABLE IF EXISTS audit_event CASCADE;
DROP TYPE IF EXISTS audit_actor_type;
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
