"""completeness engine schema (doc 04 §3.2, reconciled with doc 01): result metadata on completeness_check,
requirement_waiver, doc_request columns for needs-info reconciliation, signoff invalidation columns"""

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

UP = r"""
ALTER TABLE completeness_check
  ADD COLUMN provisional boolean NOT NULL DEFAULT false,
  ADD COLUMN blocker_count int NOT NULL DEFAULT 0,
  ADD COLUMN warning_count int NOT NULL DEFAULT 0,
  ADD COLUMN result_hash char(64) NOT NULL DEFAULT repeat('0', 64);
ALTER TABLE completeness_check ALTER COLUMN result_hash DROP DEFAULT;
UPDATE completeness_check SET trigger = 'doc_event' WHERE trigger IS NULL OR trigger NOT IN
  ('doc_event','manual','waive','reclassify','nightly','config_republish','claim_build_precheck');
ALTER TABLE completeness_check ALTER COLUMN trigger SET NOT NULL;
ALTER TABLE completeness_check ADD CONSTRAINT ck_completeness_trigger CHECK (trigger IN
  ('doc_event','manual','waive','reclassify','nightly','config_republish','claim_build_precheck'));
CREATE INDEX ix_completeness_case_latest ON completeness_check (case_id, run_no DESC);

CREATE TABLE requirement_waiver (
  id          uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  case_id     uuid NOT NULL REFERENCES claim_case(id),
  rule_id     text NOT NULL,
  doc_type    doc_type NOT NULL,
  reason      text NOT NULL,
  waived_by   uuid NOT NULL REFERENCES app_user(id),
  created_at  timestamptz NOT NULL DEFAULT now(),
  revoked_at  timestamptz,
  revoked_by  uuid REFERENCES app_user(id),
  CONSTRAINT ck_requirement_waiver_reason CHECK (length(reason) >= 10)
);
CREATE UNIQUE INDEX uq_requirement_waiver_active ON requirement_waiver (case_id, rule_id) WHERE revoked_at IS NULL;

ALTER TABLE doc_request
  ADD COLUMN reason_code text NOT NULL DEFAULT 'not_uploaded',
  ADD COLUMN opened_by_run int,
  ADD COLUMN last_seen_run int,
  ADD COLUMN closed_at timestamptz;
ALTER TABLE doc_request ALTER COLUMN reason_code DROP DEFAULT;
ALTER TABLE doc_request ALTER COLUMN rule_id SET NOT NULL;
DROP INDEX uq_doc_request_open;
CREATE UNIQUE INDEX uq_doc_request_open ON doc_request (case_id, rule_id, reason_code) WHERE status = 'open';

ALTER TABLE signoff ADD COLUMN invalidated_at timestamptz, ADD COLUMN invalidated_reason text;
"""

DOWN = r"""
ALTER TABLE signoff DROP COLUMN invalidated_at, DROP COLUMN invalidated_reason;
DROP INDEX uq_doc_request_open;
CREATE UNIQUE INDEX uq_doc_request_open ON doc_request (case_id, doc_type) WHERE status = 'open';
ALTER TABLE doc_request ALTER COLUMN rule_id DROP NOT NULL;
ALTER TABLE doc_request DROP COLUMN reason_code, DROP COLUMN opened_by_run, DROP COLUMN last_seen_run,
  DROP COLUMN closed_at;
DROP TABLE requirement_waiver;
DROP INDEX ix_completeness_case_latest;
ALTER TABLE completeness_check DROP CONSTRAINT ck_completeness_trigger;
ALTER TABLE completeness_check ALTER COLUMN trigger DROP NOT NULL;
ALTER TABLE completeness_check DROP COLUMN provisional, DROP COLUMN blocker_count, DROP COLUMN warning_count,
  DROP COLUMN result_hash;
"""


def _run(sql: str) -> None:
    cur = op.get_bind().connection.cursor()
    cur.execute(sql)
    cur.close()


def upgrade() -> None:
    _run(UP)


def downgrade() -> None:
    _run(DOWN)
