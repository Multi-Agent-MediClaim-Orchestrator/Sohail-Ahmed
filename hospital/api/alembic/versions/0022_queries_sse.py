"""query loop schema (doc 07 §3.1, reconciled with doc 01): triage/escalation columns, richer query_response, and
supplementary-document tracking so only unsent documents are included in a response"""

from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

UP = r"""
ALTER TABLE insurer_query
  ADD COLUMN triage_source text,
  ADD COLUMN escalation_risk boolean NOT NULL DEFAULT false,
  ADD COLUMN assigned_to uuid REFERENCES app_user(id),
  ADD COLUMN revision int NOT NULL DEFAULT 1,
  ADD COLUMN closed_reason text,
  ADD CONSTRAINT ck_insurer_query_triage_source CHECK (triage_source IS NULL OR triage_source IN ('crew','rules','officer'));
DROP INDEX ix_query_open_due;
CREATE INDEX ix_query_open_due ON insurer_query(due_by) WHERE status IN ('open','draft_ready');
CREATE INDEX ix_query_inbox ON insurer_query (status, due_by, id);

ALTER TABLE query_response
  ADD COLUMN grounding jsonb,
  ADD COLUMN override_note text,
  ADD COLUMN model_info jsonb,
  ADD COLUMN approved_at timestamptz,
  ADD COLUMN created_by text NOT NULL DEFAULT 'system';
ALTER TABLE query_response ALTER COLUMN created_by DROP DEFAULT;
ALTER TABLE query_response DROP CONSTRAINT ck_query_response_source;
ALTER TABLE query_response DROP CONSTRAINT ck_query_response_status;
ALTER TABLE query_response ADD CONSTRAINT ck_query_response_source CHECK (source IN ('agent','human','human_edit'));
ALTER TABLE query_response ADD CONSTRAINT ck_query_response_status
  CHECK (status IN ('draft','needs_attention','approved','sent','superseded'));

ALTER TABLE document ADD COLUMN supplementary boolean NOT NULL DEFAULT false, ADD COLUMN sent_at timestamptz;
"""
DOWN = r"""
ALTER TABLE document DROP COLUMN supplementary, DROP COLUMN sent_at;
UPDATE query_response SET status = 'draft' WHERE status IN ('needs_attention', 'superseded');
UPDATE query_response SET source = 'agent' WHERE source = 'human';
ALTER TABLE query_response DROP CONSTRAINT ck_query_response_status, DROP CONSTRAINT ck_query_response_source;
ALTER TABLE query_response ADD CONSTRAINT ck_query_response_source CHECK (source IN ('agent','human_edit'));
ALTER TABLE query_response ADD CONSTRAINT ck_query_response_status CHECK (status IN ('draft','approved','sent'));
ALTER TABLE query_response DROP COLUMN grounding, DROP COLUMN override_note, DROP COLUMN model_info,
  DROP COLUMN approved_at, DROP COLUMN created_by;
DROP INDEX ix_query_inbox;
ALTER TABLE insurer_query DROP CONSTRAINT ck_insurer_query_triage_source, DROP COLUMN triage_source,
  DROP COLUMN escalation_risk, DROP COLUMN assigned_to, DROP COLUMN revision, DROP COLUMN closed_reason;
"""


def _run(sql: str) -> None:
    cur = op.get_bind().connection.cursor()
    cur.execute(sql)
    cur.close()


def upgrade() -> None:
    _run(UP)


def downgrade() -> None:
    _run(DOWN)
