"""core master data: product, policy, member, utilisation, network hospital

Revision ID: 0002
Revises: 0001
"""

from alembic import op
from insurer_app.migration_helpers import add_updated_at_trigger

revision = "0002"
down_revision = "0001"

TS = "created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()"


def upgrade() -> None:
    op.execute(
        f"""CREATE TABLE core.insurance_product (
  id UUID PRIMARY KEY, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL, insurer_name TEXT NOT NULL,
  active BOOLEAN NOT NULL DEFAULT true, {TS})"""
    )
    op.execute(
        f"""CREATE TABLE core.policy (
  id UUID PRIMARY KEY, policy_number TEXT UNIQUE NOT NULL, product_id UUID NOT NULL REFERENCES core.insurance_product(id),
  policy_holder_name TEXT NOT NULL, start_date DATE NOT NULL, end_date DATE NOT NULL,
  sum_insured NUMERIC(14,2) NOT NULL CHECK (sum_insured > 0), cumulative_bonus NUMERIC(14,2) NOT NULL DEFAULT 0,
  status TEXT NOT NULL CHECK (status IN ('active','lapsed','cancelled','suspended')),
  premium_paid_until DATE, grace_days INT NOT NULL DEFAULT 30, {TS}, CHECK (end_date > start_date))"""
    )
    op.execute("CREATE INDEX ix_policy_status ON core.policy(status)")
    op.execute(
        f"""CREATE TABLE core.policy_member (
  id UUID PRIMARY KEY, policy_id UUID NOT NULL REFERENCES core.policy(id), member_id TEXT UNIQUE NOT NULL,
  full_name TEXT NOT NULL, full_name_norm TEXT NOT NULL, dob DATE NOT NULL,
  gender CHAR(1) NOT NULL CHECK (gender IN ('M','F','O')), relationship TEXT NOT NULL, id_proof_hash TEXT,
  cover_start DATE NOT NULL, pre_existing TEXT[] NOT NULL DEFAULT '{{}}', {TS})"""
    )
    op.execute("CREATE INDEX ix_member_policy ON core.policy_member(policy_id)")
    op.execute("CREATE INDEX ix_member_name_trgm ON core.policy_member USING gin (full_name_norm gin_trgm_ops)")
    op.execute(
        """CREATE TABLE core.policy_claim_utilisation (
  id UUID PRIMARY KEY, policy_id UUID NOT NULL REFERENCES core.policy(id), policy_year INT NOT NULL,
  utilised_amount NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK (utilised_amount >= 0), UNIQUE (policy_id, policy_year))"""
    )
    op.execute(
        """CREATE TABLE core.network_hospital (
  id UUID PRIMARY KEY, hospital_code TEXT UNIQUE NOT NULL, name TEXT NOT NULL, city TEXT, rohini_id TEXT,
  network_status TEXT NOT NULL CHECK (network_status IN ('network','non_network','blacklisted')),
  room_rent_tier TEXT, empanelment_valid_till DATE, hmac_key_id TEXT UNIQUE NOT NULL)"""
    )
    for t in ("insurance_product", "policy", "policy_member"):
        add_updated_at_trigger(op, "core", t)


def downgrade() -> None:
    for t in ("network_hospital", "policy_claim_utilisation", "policy_member", "policy", "insurance_product"):
        op.execute(f"DROP TABLE IF EXISTS core.{t} CASCADE")
