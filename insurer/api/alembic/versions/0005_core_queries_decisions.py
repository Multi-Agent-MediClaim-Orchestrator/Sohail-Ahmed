"""query, decision, approval, settlement (base)

Revision ID: 0005
Revises: 0004
"""

from alembic import op
from insurer_app.migration_helpers import add_updated_at_trigger

revision = "0005"
down_revision = "0004"


def upgrade() -> None:
    op.execute(
        """CREATE TABLE core.query (
  id UUID PRIMARY KEY, case_id UUID NOT NULL REFERENCES core.claim_case(id),
  round SMALLINT NOT NULL CHECK (round BETWEEN 1 AND 3), category TEXT NOT NULL, text TEXT NOT NULL,
  requested_doc_types TEXT[] NOT NULL DEFAULT '{}',
  status TEXT NOT NULL CHECK (status IN ('open','draft_ready','answered','closed','escalated')),
  origin TEXT NOT NULL CHECK (origin IN ('agent_draft','human','scripted')), draft_text TEXT, draft_citations JSONB,
  raised_by TEXT, due_by timestamptz NOT NULL, sent_at timestamptz, answered_at timestamptz, response JSONB,
  triage JSONB, callback_seq BIGINT,
  created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now())"""
    )
    op.execute("CREATE INDEX ix_query_case ON core.query(case_id, round)")
    op.execute(
        """CREATE TABLE core.decision (
  id UUID PRIMARY KEY, case_id UUID NOT NULL REFERENCES core.claim_case(id),
  kind TEXT NOT NULL CHECK (kind IN ('recommendation','pending_approval','final')),
  outcome TEXT NOT NULL CHECK (outcome IN ('approve','partial','reject','needs_info')),
  approved_amount NUMERIC(14,2), reason_codes TEXT[] NOT NULL DEFAULT '{}', deductions JSONB NOT NULL DEFAULT '[]',
  explanation TEXT, calc_result_id UUID REFERENCES core.calculation_result(id),
  gate_tier TEXT CHECK (gate_tier IN ('auto','reviewer','single_approver','dual_approver','single','dual')),
  config_versions JSONB NOT NULL, created_by TEXT NOT NULL, created_at timestamptz NOT NULL DEFAULT now())"""
    )
    op.execute(
        """CREATE TABLE core.approval (
  id UUID PRIMARY KEY, decision_id UUID NOT NULL REFERENCES core.decision(id), approver TEXT NOT NULL,
  verdict TEXT NOT NULL CHECK (verdict IN ('approve','reject','return')), comment TEXT, approver_role TEXT NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(), UNIQUE (decision_id, approver))"""
    )
    op.execute(
        """CREATE TABLE core.settlement (
  id UUID PRIMARY KEY, case_id UUID NOT NULL REFERENCES core.claim_case(id), amount NUMERIC(14,2) NOT NULL,
  mode TEXT NOT NULL DEFAULT 'neft_sim', utr TEXT UNIQUE,
  status TEXT NOT NULL CHECK (status IN ('initiated','paid','failed','reversed')),
  initiated_at timestamptz, paid_at timestamptz, failure_reason TEXT)"""
    )
    add_updated_at_trigger(op, "core", "query")


def downgrade() -> None:
    for t in ("settlement", "approval", "decision", "query"):
        op.execute(f"DROP TABLE IF EXISTS core.{t} CASCADE")
