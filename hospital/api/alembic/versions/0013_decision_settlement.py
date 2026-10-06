"""decision_settlement"""

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

UP_SQL = r"""
ALTER TABLE claim_case ADD COLUMN decision jsonb;
ALTER TABLE claim_case ADD COLUMN approved_amount numeric(14,2);
ALTER TABLE claim_case ADD COLUMN short_pay_amount numeric(14,2);
ALTER TABLE claim_case ADD COLUMN settled_amount numeric(14,2);
ALTER TABLE claim_case ADD COLUMN settled_at timestamptz;
ALTER TABLE claim_case DROP CONSTRAINT ck_case_amounts;
ALTER TABLE claim_case ADD CONSTRAINT ck_case_amounts CHECK (
        (claimed_amount IS NULL OR claimed_amount >= 0)
    AND (preauth_amount IS NULL OR preauth_amount >= 0)
    AND (approved_amount IS NULL OR approved_amount >= 0));
"""

DOWN_SQL = r"""
ALTER TABLE claim_case DROP CONSTRAINT ck_case_amounts;
ALTER TABLE claim_case ADD CONSTRAINT ck_case_amounts CHECK (
        (claimed_amount IS NULL OR claimed_amount >= 0)
    AND (preauth_amount IS NULL OR preauth_amount >= 0));
ALTER TABLE claim_case DROP COLUMN decision;
ALTER TABLE claim_case DROP COLUMN approved_amount;
ALTER TABLE claim_case DROP COLUMN short_pay_amount;
ALTER TABLE claim_case DROP COLUMN settled_amount;
ALTER TABLE claim_case DROP COLUMN settled_at;
"""


def _run(sql: str) -> None:
    # raw cursor, no params: '%' in function bodies must not be read as a placeholder
    cur = op.get_bind().connection.cursor()
    cur.execute(sql)
    cur.close()


def upgrade() -> None:
    _run(UP_SQL)


def downgrade() -> None:
    _run(DOWN_SQL)
