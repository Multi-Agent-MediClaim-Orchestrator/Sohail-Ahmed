"""decision tasks, approval extensions, decision columns (03-04 §3)

Revision ID: 0009
Revises: 0008
"""

from alembic import op

revision = "0009"
down_revision = "0008"


def upgrade() -> None:
    op.execute(
        """CREATE TABLE core.decision_task (
  id UUID PRIMARY KEY, case_id UUID NOT NULL REFERENCES core.claim_case(id),
  decision_id UUID NOT NULL REFERENCES core.decision(id),
  tier TEXT NOT NULL CHECK (tier IN ('reviewer','single_approver','dual_approver')),
  required_approvals SMALLINT NOT NULL CHECK (required_approvals IN (1,2)),
  min_senior SMALLINT NOT NULL DEFAULT 0 CHECK (min_senior IN (0,1)), allowed_roles TEXT[] NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('open','completed','returned','cancelled')),
  threshold_snapshot JSONB NOT NULL, opened_by TEXT NOT NULL, opened_at timestamptz NOT NULL DEFAULT now(),
  closed_at timestamptz, close_reason TEXT)"""
    )
    op.execute("CREATE INDEX ix_decision_task_queue ON core.decision_task(status, tier, opened_at)")
    op.execute("CREATE UNIQUE INDEX ux_decision_task_one_open ON core.decision_task(case_id) WHERE status = 'open'")
    op.execute("ALTER TABLE core.approval ADD COLUMN task_id UUID REFERENCES core.decision_task(id), ADD COLUMN valid BOOLEAN NOT NULL DEFAULT true")
    op.execute("CREATE UNIQUE INDEX ux_approval_one_vote ON core.approval(task_id, approver) WHERE task_id IS NOT NULL")
    op.execute(
        """ALTER TABLE core.decision ADD COLUMN revision SMALLINT NOT NULL DEFAULT 1, ADD COLUMN override_reason TEXT,
  ADD COLUMN gate_amount NUMERIC(14,2), ADD COLUMN note TEXT, ADD COLUMN flags JSONB NOT NULL DEFAULT '[]'"""
    )
    op.execute("CREATE INDEX ix_decision_case ON core.decision(case_id, created_at DESC)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS core.ix_decision_case")
    op.execute("ALTER TABLE core.decision DROP COLUMN flags, DROP COLUMN note, DROP COLUMN gate_amount, DROP COLUMN override_reason, DROP COLUMN revision")
    op.execute("DROP INDEX IF EXISTS core.ux_approval_one_vote")
    op.execute("ALTER TABLE core.approval DROP COLUMN valid, DROP COLUMN task_id")
    op.execute("DROP TABLE IF EXISTS core.decision_task")
