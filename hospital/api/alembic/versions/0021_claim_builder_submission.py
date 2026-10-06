"""claim builder / sign-off / submission schema (doc 06 §3.1, reconciled with doc 01)"""

from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None

UP = r"""
ALTER TABLE claim_draft
  ADD COLUMN provenance jsonb NOT NULL DEFAULT '{}',
  ADD COLUMN has_errors boolean NOT NULL DEFAULT false,
  ADD COLUMN model_info jsonb,
  ADD COLUMN edit_summary jsonb;
UPDATE claim_draft SET validation = '{}' WHERE validation IS NULL;
ALTER TABLE claim_draft ALTER COLUMN validation SET DEFAULT '{}', ALTER COLUMN validation SET NOT NULL;
ALTER TABLE claim_draft DROP CONSTRAINT ck_claim_draft_source;
ALTER TABLE claim_draft ADD CONSTRAINT ck_claim_draft_source CHECK (source IN ('agent','human_edit','repair'));

ALTER TABLE signoff ADD COLUMN acknowledged_warnings text[] NOT NULL DEFAULT '{}';
CREATE UNIQUE INDEX ux_signoff_active ON signoff (draft_id) WHERE decision = 'approved' AND invalidated_at IS NULL;

ALTER TABLE outbox ADD COLUMN body_sha256 char(64) NOT NULL DEFAULT repeat('0', 64);
ALTER TABLE outbox ALTER COLUMN body_sha256 DROP DEFAULT;
ALTER TABLE outbox DROP CONSTRAINT ck_outbox_status;
ALTER TABLE outbox ADD CONSTRAINT ck_outbox_status CHECK (status IN ('pending','sending','sent','failed','dead'));
DROP INDEX ix_outbox_due;
CREATE INDEX ix_outbox_due ON outbox (next_attempt_at) WHERE status IN ('pending','sending');

ALTER TABLE inbound_callback
  ADD COLUMN response_status int NOT NULL DEFAULT 204,
  ADD COLUMN response_body jsonb;

ALTER TABLE claim_case ADD COLUMN insurer_status text, ADD COLUMN acknowledged_at timestamptz;

CREATE TABLE settlement (
  id         uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  case_id    uuid NOT NULL REFERENCES claim_case(id),
  settlement_id uuid NOT NULL,
  utr        text NOT NULL,
  amount     numeric(14,2) NOT NULL,
  tds        numeric(14,2) NOT NULL DEFAULT 0,
  mode       text NOT NULL,
  paid_on    date NOT NULL,
  raw        jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_settlement_case_utr UNIQUE (case_id, utr),
  CONSTRAINT ck_settlement_amount CHECK (amount >= 0 AND tds >= 0)
);

INSERT INTO allowed_transition (from_status, to_status) VALUES ('submitted', 'ready_for_review') ON CONFLICT DO NOTHING;
"""
DOWN = r"""
DELETE FROM allowed_transition WHERE from_status = 'submitted' AND to_status = 'ready_for_review';
DROP TABLE settlement;
ALTER TABLE claim_case DROP COLUMN insurer_status, DROP COLUMN acknowledged_at;
ALTER TABLE inbound_callback DROP COLUMN response_status, DROP COLUMN response_body;
DROP INDEX ix_outbox_due;
CREATE INDEX ix_outbox_due ON outbox (next_attempt_at) WHERE status IN ('pending','failed');
ALTER TABLE outbox DROP CONSTRAINT ck_outbox_status;
ALTER TABLE outbox ADD CONSTRAINT ck_outbox_status CHECK (status IN ('pending','sent','failed','dead'));
ALTER TABLE outbox DROP COLUMN body_sha256;
DROP INDEX ux_signoff_active;
ALTER TABLE signoff DROP COLUMN acknowledged_warnings;
ALTER TABLE claim_draft DROP CONSTRAINT ck_claim_draft_source;
ALTER TABLE claim_draft ADD CONSTRAINT ck_claim_draft_source CHECK (source IN ('agent','human_edit'));
ALTER TABLE claim_draft ALTER COLUMN validation DROP NOT NULL, ALTER COLUMN validation DROP DEFAULT;
ALTER TABLE claim_draft DROP COLUMN provenance, DROP COLUMN has_errors, DROP COLUMN model_info, DROP COLUMN edit_summary;
"""


def _run(sql: str) -> None:
    cur = op.get_bind().connection.cursor()
    cur.execute(sql)
    cur.close()


def upgrade() -> None:
    _run(UP)


def downgrade() -> None:
    _run(DOWN)
