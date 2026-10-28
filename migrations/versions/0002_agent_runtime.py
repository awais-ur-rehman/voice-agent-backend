"""Tenant-scoped runtime agents, SQL command fencing and recoverable results."""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None
TABLES = ("agents", "agent_versions", "agent_sessions", "command_receipts", "callbacks")


def upgrade():
    op.execute("""
    ALTER TABLE providers ADD COLUMN name_ur text;
    ALTER TABLE services ADD COLUMN name_ur text;
    ALTER TABLE patients ADD COLUMN name_ur text;
    CREATE TABLE agents (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      tenant_id uuid NOT NULL REFERENCES tenants ON DELETE CASCADE,
      name text NOT NULL, enabled boolean NOT NULL DEFAULT true, active_version_id uuid,
      UNIQUE(tenant_id,id)
    );
    CREATE TABLE agent_versions (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      tenant_id uuid NOT NULL REFERENCES tenants ON DELETE CASCADE,
      agent_id uuid NOT NULL, config jsonb NOT NULL DEFAULT '{}',
      created_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE(tenant_id,id), UNIQUE(tenant_id,agent_id,id),
      FOREIGN KEY(tenant_id,agent_id) REFERENCES agents(tenant_id,id)
    );
    ALTER TABLE agents ADD CONSTRAINT agent_active_version
      FOREIGN KEY(tenant_id,id,active_version_id)
      REFERENCES agent_versions(tenant_id,agent_id,id) DEFERRABLE INITIALLY DEFERRED;
    CREATE FUNCTION immutable_agent_version() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN RAISE EXCEPTION 'Published agent versions are immutable'; END $$;
    CREATE TRIGGER immutable_agent_version BEFORE UPDATE ON agent_versions
      FOR EACH ROW EXECUTE FUNCTION immutable_agent_version();
    CREATE TABLE agent_sessions (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      tenant_id uuid NOT NULL REFERENCES tenants ON DELETE CASCADE,
      agent_id uuid NOT NULL, version_id uuid NOT NULL,
      revision integer NOT NULL DEFAULT 0 CHECK(revision>=0),
      checkpoint jsonb, status text NOT NULL DEFAULT 'active'
        CHECK(status IN ('active','ended','expired')),
      token_jti_hash text NOT NULL, expires_at timestamptz NOT NULL,
      revoked_at timestamptz, lease_until timestamptz,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE(tenant_id,id),
      FOREIGN KEY(tenant_id,agent_id,version_id) REFERENCES agent_versions(tenant_id,agent_id,id)
    );
    CREATE TABLE command_receipts (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      tenant_id uuid NOT NULL REFERENCES tenants ON DELETE CASCADE,
      session_id uuid NOT NULL, command_id text NOT NULL CHECK(length(command_id) BETWEEN 1 AND 128),
      request_digest text NOT NULL, revision integer NOT NULL CHECK(revision>0),
      response jsonb, checkpoint jsonb,
      created_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz NOT NULL,
      UNIQUE(tenant_id,session_id,command_id),
      FOREIGN KEY(tenant_id,session_id) REFERENCES agent_sessions(tenant_id,id)
    );
    CREATE INDEX receipt_retention ON command_receipts(expires_at);
    CREATE TABLE callbacks (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      tenant_id uuid NOT NULL REFERENCES tenants ON DELETE CASCADE,
      session_id uuid NOT NULL, agent_id uuid NOT NULL,
      phone text, reason text NOT NULL DEFAULT 'caller_requested',
      status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','handled')),
      created_at timestamptz NOT NULL DEFAULT now(),
      FOREIGN KEY(tenant_id,session_id) REFERENCES agent_sessions(tenant_id,id),
      FOREIGN KEY(tenant_id,agent_id) REFERENCES agents(tenant_id,id)
    );
    ALTER TABLE audit_log ALTER COLUMN actor_id DROP NOT NULL;
    ALTER TABLE audit_log ADD COLUMN agent_id uuid;
    ALTER TABLE audit_log ADD CONSTRAINT audit_agent FOREIGN KEY(tenant_id,agent_id)
      REFERENCES agents(tenant_id,id);
    ALTER TABLE audit_log ADD CONSTRAINT audit_exact_actor
      CHECK ((actor_id IS NULL) <> (agent_id IS NULL));
    """)
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_scope ON {table} "
            "USING (tenant_id=NULLIF(current_setting('app.tenant_id',true),'')::uuid) "
            "WITH CHECK (tenant_id=NULLIF(current_setting('app.tenant_id',true),'')::uuid)"
        )
        op.execute(f"GRANT SELECT,INSERT,UPDATE,DELETE ON {table} TO scheduler_app")
    op.execute("REVOKE UPDATE,DELETE ON agent_versions FROM scheduler_app")


def downgrade():
    # Agent-only audit entries cannot satisfy the restored staff-only schema.
    op.execute("DELETE FROM audit_log WHERE actor_id IS NULL")
    op.execute("ALTER TABLE audit_log DROP CONSTRAINT audit_exact_actor")
    op.execute("ALTER TABLE audit_log DROP COLUMN agent_id")
    op.execute("ALTER TABLE audit_log ALTER COLUMN actor_id SET NOT NULL")
    op.execute("ALTER TABLE agents DROP CONSTRAINT agent_active_version")
    for table in reversed(TABLES):
        op.execute(f"DROP TABLE {table}")
    op.execute("DROP FUNCTION immutable_agent_version()")
    for table in ("patients", "services", "providers"):
        op.execute(f"ALTER TABLE {table} DROP COLUMN name_ur")
