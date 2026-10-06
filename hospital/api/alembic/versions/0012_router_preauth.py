"""router_preauth"""

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

UP_SQL = r"""
CREATE TABLE network_insurer (
  insurer_name text PRIMARY KEY,
  cashless_supported boolean NOT NULL,
  notes text
);

CREATE TABLE simulated_preauth (               -- pre-auth is out of scope; this simulates its record (arch. §1)
  ref text PRIMARY KEY,
  member_id text NOT NULL,
  insurer_name text NOT NULL,
  approved_amount numeric(14,2) NOT NULL,
  valid_from date NOT NULL,
  valid_to date NOT NULL,
  status text NOT NULL,
  CONSTRAINT ck_simulated_preauth_status CHECK (status IN ('approved','enhanced','cancelled','expired')),
  CONSTRAINT ck_simulated_preauth_validity CHECK (valid_to >= valid_from)
);
CREATE INDEX ix_simulated_preauth_member ON simulated_preauth(member_id);

ALTER TABLE claim_case ADD COLUMN admitted_at timestamptz;
ALTER TABLE claim_case ADD COLUMN route jsonb;
ALTER TABLE claim_case ADD COLUMN flags text[] NOT NULL DEFAULT '{}';
ALTER TABLE claim_case ADD COLUMN intimation_deadline timestamptz;
CREATE INDEX ix_case_intimation ON claim_case(intimation_deadline) WHERE intimation_deadline IS NOT NULL AND status IN ('draft','docs_pending','docs_complete','building_claim','ready_for_review');
CREATE INDEX ix_case_flags ON claim_case USING gin (flags);
"""

DOWN_SQL = r"""
DROP INDEX IF EXISTS ix_case_intimation;
DROP INDEX IF EXISTS ix_case_flags;
ALTER TABLE claim_case DROP COLUMN admitted_at;
ALTER TABLE claim_case DROP COLUMN route;
ALTER TABLE claim_case DROP COLUMN flags;
ALTER TABLE claim_case DROP COLUMN intimation_deadline;
DROP TABLE IF EXISTS simulated_preauth CASCADE;
DROP TABLE IF EXISTS network_insurer CASCADE;
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
