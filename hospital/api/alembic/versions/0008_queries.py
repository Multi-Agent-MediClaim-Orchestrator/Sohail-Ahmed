"""queries"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

UP_SQL = r"""
CREATE TABLE insurer_query (
  id uuid PRIMARY KEY,
  case_id uuid NOT NULL REFERENCES claim_case(id),
  insurer_query_id uuid NOT NULL,
  round smallint NOT NULL,
  category query_category NOT NULL,
  text text NOT NULL,
  requested_doc_types doc_type[] NOT NULL DEFAULT '{}',
  due_by timestamptz,
  status query_status NOT NULL DEFAULT 'open',
  triage jsonb,                       -- {class, urgency, owner_role, needs_docs, auto_draftable}
  overdue_notified_at timestamptz,
  responded_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_insurer_query_id UNIQUE (insurer_query_id),
  CONSTRAINT ck_insurer_query_round CHECK (round BETWEEN 1 AND 3)
);
CREATE INDEX ix_query_case ON insurer_query(case_id, round);
CREATE INDEX ix_query_open_due ON insurer_query(due_by) WHERE status IN ('open','draft_ready');

CREATE TABLE query_response (
  id uuid PRIMARY KEY,
  query_id uuid NOT NULL REFERENCES insurer_query(id),
  version int NOT NULL,
  draft_text text NOT NULL,
  citations jsonb,                    -- [{doc_id,page,quote}|{kb_id,chunk,quote}]
  unsupported_claims jsonb,           -- output of grounding self-check (doc 09)
  attached_doc_ids uuid[] NOT NULL DEFAULT '{}',
  source text NOT NULL,
  status text NOT NULL DEFAULT 'draft',
  approved_by uuid REFERENCES app_user(id),
  second_approver uuid REFERENCES app_user(id),   -- round 3 two-person rule (doc 07)
  sent_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_query_response_version UNIQUE (query_id, version),
  CONSTRAINT ck_query_response_source CHECK (source IN ('agent','human_edit')),
  CONSTRAINT ck_query_response_status CHECK (status IN ('draft','approved','sent')),
  CONSTRAINT ck_query_response_distinct CHECK (second_approver IS NULL OR second_approver <> approved_by)
);

CREATE TRIGGER trg_insurer_query_updated BEFORE UPDATE ON insurer_query
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();
"""

DOWN_SQL = r"""
DROP TABLE IF EXISTS query_response CASCADE;
DROP TABLE IF EXISTS insurer_query CASCADE;
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
