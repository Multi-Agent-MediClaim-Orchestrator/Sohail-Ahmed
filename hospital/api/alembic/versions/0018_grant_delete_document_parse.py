"""reparse deletes derived parse passes (doc 03 §4.2); document_parse is derived data, so the app role
may delete it (doc 01 §3.5 granted DELETE only on reminder/doc_request/outbox - found by test)"""

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT DELETE ON document_parse TO hosp_app")


def downgrade() -> None:
    op.execute("REVOKE DELETE ON document_parse FROM hosp_app")
