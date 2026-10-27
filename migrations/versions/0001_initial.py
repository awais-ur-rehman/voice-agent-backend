"""Frozen Phase 1 PostgreSQL schema and security boundary."""

from pathlib import Path

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None
TABLES = [
    "tenants",
    "users",
    "refresh_tokens",
    "providers",
    "services",
    "provider_services",
    "availability_rules",
    "availability_exceptions",
    "patients",
    "appointments",
    "idempotency_records",
    "audit_log",
]


def upgrade():
    op.execute(Path(__file__).with_name("0001_schema.sql").read_text())
    for table in TABLES:
        column = "id" if table == "tenants" else "tenant_id"
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_scope ON {table} USING ({column}=NULLIF(current_setting('app.tenant_id',true),'')::uuid) WITH CHECK ({column}=NULLIF(current_setting('app.tenant_id',true),'')::uuid)"
        )
        op.execute(f"GRANT SELECT,INSERT,UPDATE,DELETE ON {table} TO scheduler_app")
    op.execute("REVOKE INSERT,DELETE ON tenants FROM scheduler_app")
    op.execute("REVOKE UPDATE,DELETE ON audit_log FROM scheduler_app")
    op.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
    op.execute("GRANT USAGE ON SCHEMA public TO scheduler_app")
    op.execute("""CREATE FUNCTION auth_lookup(login_email text)
    RETURNS TABLE(id uuid, tenant_id uuid, email text, password_hash text, role text, active boolean)
    LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
    SELECT u.id,u.tenant_id,u.email,u.password_hash,u.role,u.active FROM public.users u
    WHERE lower(u.email)=lower(login_email) AND u.active=true LIMIT 1 $$""")
    op.execute("REVOKE ALL ON FUNCTION auth_lookup(text) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION auth_lookup(text) TO scheduler_app")


def downgrade():
    op.execute("DROP FUNCTION auth_lookup(text)")
    for table in reversed(TABLES):
        op.execute(f"DROP TABLE {table} CASCADE")
    op.execute("DROP FUNCTION appointment_boundaries()")
