"""settlement extensions, events, tasks, reconciliation (03-06 §3)

Revision ID: 0011
Revises: 0010
"""

from alembic import op

revision = "0011"
down_revision = "0010"


def upgrade() -> None:
    op.execute("ALTER TABLE core.settlement DROP CONSTRAINT IF EXISTS settlement_case_id_key")
    op.execute(
        """ALTER TABLE core.settlement
  ADD COLUMN payee_type TEXT NOT NULL DEFAULT 'hospital' CHECK (payee_type IN ('hospital','member')),
  ADD COLUMN payee_ref TEXT NOT NULL DEFAULT '', ADD COLUMN beneficiary_account_hash CHAR(64),
  ADD COLUMN gross_amount NUMERIC(14,2) NOT NULL DEFAULT 0, ADD COLUMN adjustments JSONB NOT NULL DEFAULT '[]',
  ADD COLUMN attempt_count INT NOT NULL DEFAULT 0, ADD COLUMN idempotency_key UUID NOT NULL DEFAULT gen_random_uuid(),
  ADD COLUMN next_retry_at timestamptz, ADD COLUMN release_utilisation BOOLEAN NOT NULL DEFAULT false,
  ADD COLUMN created_at timestamptz NOT NULL DEFAULT now()"""
    )
    op.execute("ALTER TABLE core.settlement ALTER COLUMN mode SET DEFAULT 'neft_sim'")
    op.execute("ALTER TABLE core.settlement ADD CONSTRAINT ck_settlement_mode CHECK (mode IN ('neft_sim','imps_sim','rtgs_sim'))")
    op.execute("DROP INDEX IF EXISTS core.ux_settlement_utr")
    op.execute("CREATE UNIQUE INDEX ux_settlement_utr ON core.settlement(utr) WHERE utr IS NOT NULL")
    op.execute("CREATE UNIQUE INDEX ux_settlement_active_case ON core.settlement(case_id) WHERE status IN ('initiated','paid','failed')")
    op.execute(
        """CREATE TABLE core.settlement_event (
  id UUID PRIMARY KEY, settlement_id UUID NOT NULL REFERENCES core.settlement(id), event TEXT NOT NULL, detail JSONB,
  created_at timestamptz NOT NULL DEFAULT now())"""
    )
    op.execute("CREATE INDEX ix_settlement_event_s ON core.settlement_event(settlement_id, created_at)")
    op.execute(
        """CREATE TABLE core.settlement_task (
  id UUID PRIMARY KEY, settlement_id UUID REFERENCES core.settlement(id), case_id UUID REFERENCES core.claim_case(id),
  kind TEXT NOT NULL CHECK (kind IN ('settlement_failed','reversal','anomaly','refund_due')),
  status TEXT NOT NULL CHECK (status IN ('open','done','cancelled')) DEFAULT 'open', detail JSONB,
  opened_at timestamptz NOT NULL DEFAULT now(), closed_at timestamptz, closed_by TEXT)"""
    )
    op.execute(
        """CREATE TABLE core.reconciliation_run (
  id UUID PRIMARY KEY, day DATE NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('settlement','utilisation')),
  diffs JSONB NOT NULL, created_at timestamptz NOT NULL DEFAULT now())"""
    )


def downgrade() -> None:
    for t in ("reconciliation_run", "settlement_task", "settlement_event"):
        op.execute(f"DROP TABLE IF EXISTS core.{t}")
    op.execute("DROP INDEX IF EXISTS core.ux_settlement_active_case")
    op.execute("ALTER TABLE core.settlement DROP CONSTRAINT IF EXISTS ck_settlement_mode")
    op.execute(
        "ALTER TABLE core.settlement DROP COLUMN created_at, DROP COLUMN release_utilisation, DROP COLUMN next_retry_at, "
        "DROP COLUMN idempotency_key, DROP COLUMN attempt_count, DROP COLUMN adjustments, DROP COLUMN gross_amount, "
        "DROP COLUMN beneficiary_account_hash, DROP COLUMN payee_ref, DROP COLUMN payee_type"
    )
