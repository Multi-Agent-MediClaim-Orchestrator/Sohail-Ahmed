"""extensions_helpers"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

UP_SQL = r"""
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS citext;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS btree_gist;

GRANT USAGE ON SCHEMA public TO hosp_app, hosp_readonly;
ALTER DEFAULT PRIVILEGES FOR ROLE hosp_owner IN SCHEMA public GRANT SELECT, INSERT, UPDATE ON TABLES TO hosp_app;
ALTER DEFAULT PRIVILEGES FOR ROLE hosp_owner IN SCHEMA public GRANT SELECT ON TABLES TO hosp_readonly;
ALTER DEFAULT PRIVILEGES FOR ROLE hosp_owner IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO hosp_app;

CREATE FUNCTION set_updated_at() RETURNS trigger AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END $$ LANGUAGE plpgsql;

CREATE FUNCTION forbid_mutation() RETURNS trigger AS $$
BEGIN RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE = '42501'; END $$ LANGUAGE plpgsql;

CREATE FUNCTION forbid_version_regress() RETURNS trigger AS $$
BEGIN IF NEW.version < OLD.version THEN RAISE EXCEPTION 'version_regress'; END IF; RETURN NEW; END $$ LANGUAGE plpgsql;

-- UUIDv7 (48-bit ms timestamp + random), for manual inserts and seeds; the app generates ids itself.
CREATE FUNCTION uuid_generate_v7() RETURNS uuid AS $$
  SELECT encode(
    set_bit(set_bit(
      overlay(uuid_send(gen_random_uuid())
              placing substring(int8send(floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint) from 3)
              from 1 for 6),
      52, 1), 53, 1), 'hex')::uuid;
$$ LANGUAGE sql VOLATILE;
"""

DOWN_SQL = r"""
DROP FUNCTION IF EXISTS uuid_generate_v7();
DROP FUNCTION IF EXISTS forbid_version_regress();
DROP FUNCTION IF EXISTS forbid_mutation();
DROP FUNCTION IF EXISTS set_updated_at();
ALTER DEFAULT PRIVILEGES FOR ROLE hosp_owner IN SCHEMA public REVOKE SELECT, INSERT, UPDATE ON TABLES FROM hosp_app;
ALTER DEFAULT PRIVILEGES FOR ROLE hosp_owner IN SCHEMA public REVOKE SELECT ON TABLES FROM hosp_readonly;
ALTER DEFAULT PRIVILEGES FOR ROLE hosp_owner IN SCHEMA public REVOKE USAGE, SELECT ON SEQUENCES FROM hosp_app;
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
