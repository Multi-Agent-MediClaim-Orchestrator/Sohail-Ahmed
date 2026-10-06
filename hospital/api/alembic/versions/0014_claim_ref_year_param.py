"""next_claim_ref(p_year): optional year argument so year rollover is testable without freezing time"""

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

UP = r"""
DROP FUNCTION IF EXISTS next_claim_ref();
CREATE FUNCTION next_claim_ref(p_year int DEFAULT NULL) RETURNS text AS $$
DECLARE
  y int := COALESCE(p_year, extract(year FROM now() AT TIME ZONE 'UTC')::int);
  s text := 'claim_ref_seq_' || COALESCE(p_year, extract(year FROM now() AT TIME ZONE 'UTC')::int);
  n bigint;
BEGIN
  IF to_regclass(s) IS NULL THEN  -- lazy per-year sequence; the lock stops concurrent first callers racing in pg_class
    PERFORM pg_advisory_xact_lock(hashtext('next_claim_ref'));
    EXECUTE format('CREATE SEQUENCE IF NOT EXISTS %I', s);
  END IF;
  EXECUTE format('SELECT nextval(%L)', s) INTO n;
  RETURN 'HC-' || y || '-' || lpad(n::text, 6, '0');
END $$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = public;
REVOKE ALL ON FUNCTION next_claim_ref(int) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION next_claim_ref(int) TO hosp_app;
"""

DOWN = r"""
DROP FUNCTION IF EXISTS next_claim_ref(int);
CREATE FUNCTION next_claim_ref() RETURNS text AS $$
DECLARE y int := extract(year FROM now() AT TIME ZONE 'UTC'); s text := 'claim_ref_seq_' || y; n bigint;
BEGIN
  IF to_regclass(s) IS NULL THEN
    PERFORM pg_advisory_xact_lock(hashtext('next_claim_ref'));
    EXECUTE format('CREATE SEQUENCE IF NOT EXISTS %I', s);
  END IF;
  EXECUTE format('SELECT nextval(%L)', s) INTO n;
  RETURN 'HC-' || y || '-' || lpad(n::text, 6, '0');
END $$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = public;
REVOKE ALL ON FUNCTION next_claim_ref() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION next_claim_ref() TO hosp_app;
"""


def _run(sql: str) -> None:
    cur = op.get_bind().connection.cursor()
    cur.execute(sql)
    cur.close()


def upgrade() -> None:
    _run(UP)


def downgrade() -> None:
    _run(DOWN)
