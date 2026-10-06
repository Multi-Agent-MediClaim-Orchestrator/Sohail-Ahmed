"""reminders"""

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

UP_SQL = r"""
CREATE TABLE reminder (
  id uuid PRIMARY KEY,
  case_id uuid NOT NULL REFERENCES claim_case(id),
  kind text NOT NULL,              -- doc_request | query_sla | filing_deadline | intimation | submission_nudge
  ref_id uuid,                     -- doc_request.id / insurer_query.id when applicable
  channel text NOT NULL DEFAULT 'in_app',
  fire_at timestamptz NOT NULL,
  status text NOT NULL DEFAULT 'scheduled',
  attempts smallint NOT NULL DEFAULT 0,
  fired_at timestamptz,
  last_error text,
  payload jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_reminder_kind CHECK (kind IN ('doc_request','query_sla','filing_deadline','intimation','submission_nudge')),
  CONSTRAINT ck_reminder_channel CHECK (channel IN ('in_app','email')),
  CONSTRAINT ck_reminder_status CHECK (status IN ('scheduled','fired','cancelled','failed')),
  CONSTRAINT uq_reminder_dedup UNIQUE (case_id, kind, ref_id, fire_at)
);
CREATE INDEX ix_reminder_due ON reminder(fire_at) WHERE status = 'scheduled';

GRANT DELETE ON reminder TO hosp_app;
"""

DOWN_SQL = r"""
DROP TABLE IF EXISTS reminder CASCADE;
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
