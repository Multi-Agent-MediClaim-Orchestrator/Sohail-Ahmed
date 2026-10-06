"""grant read on alembic_version: it is created before 0001 sets default privileges, so the app role
could not run the /v1/ready schema check (found by test_ready_and_health)"""

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT SELECT ON alembic_version TO hosp_app, hosp_readonly")


def downgrade() -> None:
    op.execute("REVOKE SELECT ON alembic_version FROM hosp_app, hosp_readonly")
