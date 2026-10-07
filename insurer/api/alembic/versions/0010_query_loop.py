"""query loop: rounds, responses, draft/lint columns (03-05 §3)

Revision ID: 0010
Revises: 0009
"""

from alembic import op

revision = "0010"
down_revision = "0009"


def upgrade() -> None:
    op.execute(
        """ALTER TABLE core.query ADD COLUMN finding_keys TEXT[] NOT NULL DEFAULT '{}',
  ADD COLUMN auto_send BOOLEAN NOT NULL DEFAULT false,
  ADD COLUMN draft_source TEXT CHECK (draft_source IN ('llm','template')),
  ADD COLUMN lint_errors JSONB NOT NULL DEFAULT '[]', ADD COLUMN acked_at timestamptz,
  ADD COLUMN is_extension BOOLEAN NOT NULL DEFAULT false, ADD COLUMN regen_count SMALLINT NOT NULL DEFAULT 0,
  ADD COLUMN closed_reason TEXT"""
    )
    op.execute("ALTER TABLE core.query ALTER COLUMN draft_citations SET DEFAULT '[]'")
    op.execute("ALTER TABLE core.query DROP CONSTRAINT IF EXISTS query_origin_check")
    op.execute("ALTER TABLE core.query ADD CONSTRAINT query_origin_check CHECK (origin IN ('agent_draft','human','scripted','template'))")
    op.execute("ALTER TABLE core.query ALTER COLUMN round DROP NOT NULL")  # extension query keeps round 3; kept non-null below
    op.execute("ALTER TABLE core.query ALTER COLUMN round SET NOT NULL")
    op.execute(
        """CREATE TABLE core.query_round (
  case_id UUID NOT NULL REFERENCES core.claim_case(id), round SMALLINT NOT NULL CHECK (round BETWEEN 1 AND 3),
  opened_at timestamptz NOT NULL, due_by timestamptz NOT NULL, closed_at timestamptz,
  outcome TEXT CHECK (outcome IN ('resolved','partially_resolved','unanswered','escalated')), PRIMARY KEY (case_id, round))"""
    )
    op.execute(
        """CREATE TABLE core.query_response (
  id UUID PRIMARY KEY, query_id UUID NOT NULL REFERENCES core.query(id), answer_text TEXT NOT NULL,
  attached_doc_ids UUID[] NOT NULL DEFAULT '{}', responded_by TEXT NOT NULL, received_at timestamptz NOT NULL DEFAULT now(),
  idempotency_key UUID NOT NULL, triage JSONB, triage_source TEXT CHECK (triage_source IN ('llm','rules','reviewer_override')),
  amends UUID REFERENCES core.query_response(id))"""
    )
    op.execute("CREATE UNIQUE INDEX ux_qresp_idem ON core.query_response(query_id, idempotency_key)")
    op.execute(
        """CREATE TABLE core.escalation (
  id UUID PRIMARY KEY, case_id UUID NOT NULL REFERENCES core.claim_case(id), reason TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('open','resolved')) DEFAULT 'open', pack JSONB NOT NULL,
  opened_at timestamptz NOT NULL DEFAULT now(), resolved_at timestamptz, resolved_by TEXT, action TEXT, note TEXT)"""
    )
    op.execute("CREATE UNIQUE INDEX ux_escalation_open ON core.escalation(case_id) WHERE status = 'open'")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS core.escalation")
    op.execute("DROP TABLE IF EXISTS core.query_response")
    op.execute("DROP TABLE IF EXISTS core.query_round")
    op.execute("ALTER TABLE core.query DROP CONSTRAINT IF EXISTS query_origin_check")
    op.execute("ALTER TABLE core.query ADD CONSTRAINT query_origin_check CHECK (origin IN ('agent_draft','human','scripted'))")
    op.execute(
        "ALTER TABLE core.query DROP COLUMN closed_reason, DROP COLUMN regen_count, DROP COLUMN is_extension, DROP COLUMN acked_at, "
        "DROP COLUMN lint_errors, DROP COLUMN draft_source, DROP COLUMN auto_send, DROP COLUMN finding_keys"
    )
