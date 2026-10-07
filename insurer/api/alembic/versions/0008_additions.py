"""columns/tables added by sibling docs (01-insurer-db §3.5)

Revision ID: 0008
Revises: 0007
"""

from alembic import op
from app.migration_helpers import add_updated_at_trigger, drop_updated_at_trigger  # noqa: F401

revision = "0008"
down_revision = "0007"


def upgrade() -> None:
    op.execute("ALTER TABLE core.network_hospital ADD COLUMN callback_base_url TEXT")
    op.execute("ALTER TABLE core.network_hospital ADD COLUMN active BOOLEAN NOT NULL DEFAULT true")
    op.execute("ALTER TABLE core.network_hospital ADD COLUMN account_hash TEXT")  # synthetic 'SIM...' beneficiary hash (06)
    op.execute("ALTER TABLE core.network_hospital ADD COLUMN watchlist BOOLEAN NOT NULL DEFAULT false")  # 04 review flag
    op.execute("ALTER TABLE core.claim_case ADD COLUMN latest_run_id UUID REFERENCES core.verification_run(id)")
    op.execute("ALTER TABLE core.claim_case ADD COLUMN degraded BOOLEAN NOT NULL DEFAULT false")
    op.execute("ALTER TABLE core.claim_case ADD COLUMN sla_breached BOOLEAN NOT NULL DEFAULT false")
    op.execute("ALTER TABLE core.claim_case ADD COLUMN procedure_group TEXT")
    op.execute("ALTER TABLE core.claim_case ADD COLUMN etag BIGINT NOT NULL DEFAULT 1")
    op.execute("ALTER TABLE core.claim_case ADD COLUMN journey_id UUID")
    op.execute("ALTER TABLE core.claim_case ADD COLUMN config_snapshot JSONB NOT NULL DEFAULT '{}'")
    op.execute("ALTER TABLE core.claim_case ADD COLUMN pending_rerun BOOLEAN NOT NULL DEFAULT false")
    op.execute(
        """CREATE FUNCTION core.bump_etag() RETURNS trigger AS $$
BEGIN NEW.etag = OLD.etag + 1; RETURN NEW; END $$ LANGUAGE plpgsql"""
    )
    op.execute("CREATE TRIGGER trg_case_etag BEFORE UPDATE ON core.claim_case FOR EACH ROW EXECUTE FUNCTION core.bump_etag()")
    op.execute("ALTER TABLE core.claim_document ADD COLUMN source_url_enc TEXT")  # Fernet-encrypted presigned URL (bearer credential)
    op.execute("ALTER TABLE core.claim_document ADD COLUMN vision JSONB")  # stamp/signature/tamper results (03 authenticity)
    op.execute("ALTER TABLE core.claim_document ADD COLUMN extract JSONB")  # typed, masked extraction from doc-pipeline
    op.execute(
        """CREATE TABLE core.finding_override (
  id UUID PRIMARY KEY, case_id UUID NOT NULL REFERENCES core.claim_case(id),
  run_id UUID NOT NULL REFERENCES core.verification_run(id), finding_key TEXT NOT NULL, finding_code TEXT NOT NULL,
  reason TEXT NOT NULL CHECK (length(reason) >= 10), overridden_by TEXT NOT NULL, role TEXT NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(), UNIQUE (case_id, finding_key))"""
    )
    op.execute("ALTER TABLE core.query ADD COLUMN dedupe_key CHAR(64)")
    op.execute("CREATE UNIQUE INDEX ux_query_open_dedupe ON core.query(case_id, dedupe_key) WHERE dedupe_key IS NOT NULL AND status IN ('open','draft_ready')")
    op.execute("ALTER TABLE core.query ADD COLUMN reminder_count SMALLINT NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE core.decision ADD COLUMN supersedes UUID REFERENCES core.decision(id)")
    op.execute(
        """ALTER TABLE core.decision ADD COLUMN status TEXT NOT NULL DEFAULT 'proposed'
  CHECK (status IN ('proposed','awaiting_approval','finalised','returned','withdrawn'))"""
    )
    op.execute(
        """CREATE TABLE core.agent_run (
  id UUID PRIMARY KEY, case_id UUID NOT NULL REFERENCES core.claim_case(id), request_id UUID NOT NULL,
  agent TEXT NOT NULL, prompt_version TEXT, model_alias TEXT, trace_id TEXT, degraded BOOLEAN NOT NULL DEFAULT false,
  output JSONB, error TEXT, created_at timestamptz NOT NULL DEFAULT now(), UNIQUE (case_id, request_id))"""
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS core.agent_run")
    op.execute("ALTER TABLE core.decision DROP COLUMN status, DROP COLUMN supersedes")
    op.execute("DROP INDEX IF EXISTS core.ux_query_open_dedupe")
    op.execute("ALTER TABLE core.query DROP COLUMN reminder_count, DROP COLUMN dedupe_key")
    op.execute("DROP TABLE IF EXISTS core.finding_override")
    op.execute("ALTER TABLE core.claim_document DROP COLUMN extract, DROP COLUMN vision, DROP COLUMN source_url_enc")
    op.execute("DROP TRIGGER IF EXISTS trg_case_etag ON core.claim_case")
    op.execute("DROP FUNCTION IF EXISTS core.bump_etag()")
    op.execute(
        "ALTER TABLE core.claim_case DROP COLUMN pending_rerun, DROP COLUMN config_snapshot, DROP COLUMN journey_id, "
        "DROP COLUMN etag, DROP COLUMN procedure_group, DROP COLUMN sla_breached, DROP COLUMN degraded, DROP COLUMN latest_run_id"
    )
    op.execute("ALTER TABLE core.network_hospital DROP COLUMN watchlist, DROP COLUMN account_hash, DROP COLUMN active, DROP COLUMN callback_base_url")
