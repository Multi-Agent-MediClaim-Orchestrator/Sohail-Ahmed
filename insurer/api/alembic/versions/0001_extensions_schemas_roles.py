"""extensions, schemas, helper functions, roles

Revision ID: 0001
Revises:
"""

import os

from alembic import op

revision = "0001"
down_revision = None


def upgrade() -> None:
    app_pw = os.environ.get("INS_APP_PASSWORD", "ins_app_dev").replace("'", "''")
    ro_pw = os.environ.get("INS_RO_PASSWORD", "ins_ro_dev").replace("'", "''")
    for ext in ("pgcrypto", "pg_trgm", "btree_gist"):
        op.execute(f"CREATE EXTENSION IF NOT EXISTS {ext}")
    for s in ("core", "config", "audit", "ops"):
        op.execute(f"CREATE SCHEMA IF NOT EXISTS {s}")
    op.execute(
        """CREATE OR REPLACE FUNCTION public.set_updated_at() RETURNS trigger AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END $$ LANGUAGE plpgsql"""
    )
    # uuid v7 fallback for SQL seeds (the app generates uuid7 itself)
    op.execute(
        """CREATE OR REPLACE FUNCTION public.uuid_generate_v7() RETURNS uuid AS $$
DECLARE ts_ms bigint := (extract(epoch from clock_timestamp()) * 1000)::bigint; b bytea;
BEGIN
  b := decode(lpad(to_hex(ts_ms), 12, '0'), 'hex') || gen_random_bytes(10);
  b := set_byte(b, 6, (get_byte(b, 6) & 15) | 112);
  b := set_byte(b, 8, (get_byte(b, 8) & 63) | 128);
  RETURN encode(b, 'hex')::uuid;
END $$ LANGUAGE plpgsql"""
    )
    op.execute(
        f"""DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ins_app') THEN CREATE ROLE ins_app LOGIN PASSWORD '{app_pw}'; END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ins_readonly') THEN CREATE ROLE ins_readonly LOGIN PASSWORD '{ro_pw}'; END IF;
END $$"""
    )
    op.execute("GRANT USAGE ON SCHEMA core, config, audit, ops TO ins_app, ins_readonly")
    op.execute("GRANT EXECUTE ON FUNCTION public.set_updated_at(), public.uuid_generate_v7() TO ins_app")
    # default privileges so later tables are covered (grants in 6.8 of the doc)
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA core, config GRANT SELECT, INSERT, UPDATE ON TABLES TO ins_app")
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA core, config, audit, ops GRANT SELECT ON TABLES TO ins_readonly")
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA core, config, audit, ops GRANT USAGE, SELECT ON SEQUENCES TO ins_app")


def downgrade() -> None:
    # Roles/extensions are cluster-level and intentionally kept (0001 is the documented non-reversible migration).
    for s in ("ops", "audit", "config", "core"):
        op.execute(f"DROP SCHEMA IF EXISTS {s} CASCADE")
    op.execute("DROP FUNCTION IF EXISTS public.set_updated_at() CASCADE")
    op.execute("DROP FUNCTION IF EXISTS public.uuid_generate_v7()")
