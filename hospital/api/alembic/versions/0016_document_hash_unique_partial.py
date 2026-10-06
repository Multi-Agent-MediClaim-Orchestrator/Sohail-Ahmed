"""duplicate-upload guard must not block re-uploading a deleted document: make (case_id, sha256) unique
only while the document is not deleted (found while designing doc 03's delete/re-upload flow)"""

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE document DROP CONSTRAINT uq_document_case_sha")
    op.execute(
        "CREATE UNIQUE INDEX uq_document_case_sha ON document (case_id, sha256) WHERE lifecycle <> 'deleted'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX uq_document_case_sha")
    op.execute("ALTER TABLE document ADD CONSTRAINT uq_document_case_sha UNIQUE (case_id, sha256)")
