"""document fetch diagnostics

Revision ID: 0013
Revises: 0012
"""

from alembic import op

revision = "0013"
down_revision = "0012"


def upgrade() -> None:
    op.execute("ALTER TABLE core.claim_case ADD COLUMN last_inbound_seq BIGINT NOT NULL DEFAULT 1")
    op.execute("ALTER TABLE core.decision ADD COLUMN finalised_at timestamptz, ADD COLUMN reviewer_ids TEXT[] NOT NULL DEFAULT '{}'")
    op.execute("ALTER TABLE core.claim_document ADD COLUMN fetch_error TEXT, ADD COLUMN refresh_attempts INT NOT NULL DEFAULT 0, "
               "ADD COLUMN scan_result TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE core.decision DROP COLUMN reviewer_ids, DROP COLUMN finalised_at")
    op.execute("ALTER TABLE core.claim_case DROP COLUMN last_inbound_seq")
    op.execute("ALTER TABLE core.claim_document DROP COLUMN scan_result, DROP COLUMN refresh_attempts, DROP COLUMN fetch_error")
