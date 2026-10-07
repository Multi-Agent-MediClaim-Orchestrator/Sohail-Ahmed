"""admissible-amount estimate on each agent claim draft (hospital-crew Policy Estimate crew): advice for the billing
officer before sign-off, never a gate"""

from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE claim_draft ADD COLUMN estimate jsonb")


def downgrade() -> None:
    op.execute("ALTER TABLE claim_draft DROP COLUMN estimate")
