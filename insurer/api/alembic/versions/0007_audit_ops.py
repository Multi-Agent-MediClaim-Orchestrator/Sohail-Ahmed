"""audit (contract 01-04 DDL), idempotency, outbox, user_profile, claim number sequence

Revision ID: 0007
Revises: 0006
"""

from alembic import op
from claim_contract.audit_ddl import audit_ddl_pg

revision = "0007"
down_revision = "0006"


def upgrade() -> None:
    for stmt in audit_ddl_pg("audit", "ins_app"):
        op.execute(stmt)
    op.execute(
        """CREATE TABLE ops.idempotency_record (
  key_id TEXT NOT NULL, idempotency_key UUID NOT NULL, request_hash CHAR(64) NOT NULL, response_status INT NOT NULL,
  response_body JSONB NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (key_id, idempotency_key))"""
    )
    op.execute(
        """CREATE TABLE ops.outbox (
  id UUID PRIMARY KEY, case_id UUID NOT NULL, endpoint TEXT NOT NULL, payload JSONB NOT NULL, seq BIGINT NOT NULL,
  idempotency_key UUID NOT NULL, status TEXT NOT NULL CHECK (status IN ('pending','sent','dead')),
  attempts INT NOT NULL DEFAULT 0, next_attempt_at timestamptz NOT NULL DEFAULT now(), last_error TEXT,
  created_at timestamptz NOT NULL DEFAULT now(), sent_at timestamptz,
  kind TEXT NOT NULL DEFAULT 'callback', base_url TEXT)"""
    )
    op.execute("CREATE INDEX ix_outbox_due ON ops.outbox(next_attempt_at) WHERE status = 'pending'")
    op.execute(
        """CREATE TABLE ops.user_profile (
  sub TEXT PRIMARY KEY, display_name TEXT NOT NULL, email TEXT, roles TEXT[] NOT NULL, active BOOLEAN NOT NULL DEFAULT true,
  last_seen_at timestamptz, skill_tags TEXT[] NOT NULL DEFAULT '{}', on_leave BOOLEAN NOT NULL DEFAULT false,
  last_assigned_at timestamptz)"""
    )
    op.execute("CREATE SEQUENCE ops.claim_no_seq START 1")
    # grants (01-insurer-db §6.8): app role never deletes from core/audit; may delete idempotency + outbox rows
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ops.idempotency_record, ops.outbox TO ins_app")
    op.execute("GRANT SELECT, INSERT, UPDATE ON ops.user_profile TO ins_app")
    op.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA ops TO ins_app")
    op.execute("GRANT SELECT ON ALL TABLES IN SCHEMA core, config, audit, ops TO ins_readonly")
    op.execute("GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA core, config TO ins_app")
    op.execute("GRANT SELECT, INSERT ON audit.audit_event TO ins_app")


def downgrade() -> None:
    for t in ("user_profile", "outbox", "idempotency_record"):
        op.execute(f"DROP TABLE IF EXISTS ops.{t} CASCADE")
    op.execute("DROP SEQUENCE IF EXISTS ops.claim_no_seq")
    for t in ("audit_anchor", "case_audit_head", "audit_event"):
        op.execute(f"DROP TABLE IF EXISTS audit.{t} CASCADE")
    op.execute("DROP FUNCTION IF EXISTS audit.audit_event_block() CASCADE")
    op.execute("DROP TYPE IF EXISTS audit_actor_type CASCADE")
