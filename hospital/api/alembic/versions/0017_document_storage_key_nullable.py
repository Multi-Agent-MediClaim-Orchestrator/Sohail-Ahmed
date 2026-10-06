"""document.storage_key must be nullable: purged tombstones null the key but keep the row for audit
(doc 03 task 15; the NOT NULL in doc 01 made the purge job impossible - found by test)"""

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE document ALTER COLUMN storage_key DROP NOT NULL")


def downgrade() -> None:
    op.execute("UPDATE document SET storage_key = 'purged/' || id::text WHERE storage_key IS NULL")
    op.execute("ALTER TABLE document ALTER COLUMN storage_key SET NOT NULL")
