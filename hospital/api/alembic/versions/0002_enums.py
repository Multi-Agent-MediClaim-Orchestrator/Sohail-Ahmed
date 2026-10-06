"""enums"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

UP_SQL = r"""

"""

DOWN_SQL = r"""
DROP TYPE IF EXISTS claim_type;
DROP TYPE IF EXISTS admission_type;
DROP TYPE IF EXISTS hospital_case_status;
DROP TYPE IF EXISTS doc_type;
DROP TYPE IF EXISTS query_category;
DROP TYPE IF EXISTS query_status;
"""


def _run(sql: str) -> None:
    # raw cursor, no params: '%' in function bodies must not be read as a placeholder
    cur = op.get_bind().connection.cursor()
    cur.execute(sql)
    cur.close()


def upgrade() -> None:
    _run(UP_SQL)
    from claim_contract import enums as _e

    for cls_name, type_name in [
        ("ClaimType", "claim_type"),
        ("AdmissionType", "admission_type"),
        ("HospitalCaseStatus", "hospital_case_status"),
        ("DocType", "doc_type"),
        ("QueryCategory", "query_category"),
        ("QueryStatus", "query_status"),
    ]:
        labels = ", ".join(f"'{m.value}'" for m in getattr(_e, cls_name))
        _run(f"CREATE TYPE {type_name} AS ENUM ({labels})")


def downgrade() -> None:
    _run(DOWN_SQL)
