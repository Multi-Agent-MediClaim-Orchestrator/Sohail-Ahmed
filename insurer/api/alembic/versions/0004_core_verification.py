"""verification_run, verification_step, calculation_result

Revision ID: 0004
Revises: 0003
"""

from alembic import op

revision = "0004"
down_revision = "0003"


def upgrade() -> None:
    op.execute(
        """CREATE TABLE core.verification_run (
  id UUID PRIMARY KEY, case_id UUID NOT NULL REFERENCES core.claim_case(id), run_no INT NOT NULL,
  trigger TEXT NOT NULL, status TEXT NOT NULL CHECK (status IN ('running','completed','failed')),
  started_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz, config_versions JSONB NOT NULL,
  client_token TEXT, superseded BOOLEAN NOT NULL DEFAULT false, outcome TEXT,
  UNIQUE (case_id, run_no))"""
    )
    op.execute("CREATE UNIQUE INDEX ux_run_client_token ON core.verification_run(case_id, trigger, client_token) WHERE client_token IS NOT NULL")
    op.execute(
        """CREATE TABLE core.verification_step (
  id UUID PRIMARY KEY, run_id UUID NOT NULL REFERENCES core.verification_run(id),
  step TEXT NOT NULL CHECK (step IN ('document_fetch','completeness','identity','authenticity','coverage','calculation')),
  status TEXT NOT NULL CHECK (status IN ('pending','running','passed','flagged','failed','skipped')),
  score NUMERIC(4,3), findings JSONB NOT NULL DEFAULT '[]', agent_output JSONB, deterministic JSONB, trace_id TEXT,
  attempt INT NOT NULL DEFAULT 0, started_at timestamptz, finished_at timestamptz, UNIQUE (run_id, step))"""
    )
    op.execute(
        """CREATE TABLE core.calculation_result (
  id UUID PRIMARY KEY, case_id UUID NOT NULL REFERENCES core.claim_case(id),
  run_id UUID REFERENCES core.verification_run(id), engine_version TEXT NOT NULL, policy_rules_version INT NOT NULL,
  input JSONB NOT NULL, output JSONB NOT NULL, payable_amount NUMERIC(14,2) NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now())"""
    )
    op.execute("CREATE INDEX ix_step_run ON core.verification_step(run_id)")


def downgrade() -> None:
    for t in ("calculation_result", "verification_step", "verification_run"):
        op.execute(f"DROP TABLE IF EXISTS core.{t} CASCADE")
