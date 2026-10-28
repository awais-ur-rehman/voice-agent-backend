"""Voice transport binding and tenant-scoped worker generations."""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    ALTER TABLE agent_sessions ADD COLUMN transport_mode text NOT NULL DEFAULT 'text'
      CHECK (transport_mode IN ('text','voice'));
    CREATE TABLE voice_sessions (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      tenant_id uuid NOT NULL REFERENCES tenants ON DELETE CASCADE,
      session_id uuid NOT NULL, creator_id uuid NOT NULL,
      request_key text NOT NULL CHECK(length(request_key) BETWEEN 1 AND 128),
      room text NOT NULL UNIQUE, participant text NOT NULL UNIQUE,
      status text NOT NULL DEFAULT 'provisioning'
        CHECK(status IN ('provisioning','active','disconnected','ended','failed')),
      generation integer NOT NULL DEFAULT 1 CHECK(generation>0),
      worker_id uuid, worker_nonce_hash text, worker_lease_until timestamptz,
      disconnected_at timestamptz, call_deadline timestamptz NOT NULL,
      delivery jsonb, created_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE(tenant_id,id), UNIQUE(tenant_id,session_id),
      UNIQUE(tenant_id,creator_id,request_key),
      FOREIGN KEY(tenant_id,session_id) REFERENCES agent_sessions(tenant_id,id),
      FOREIGN KEY(tenant_id,creator_id) REFERENCES users(tenant_id,id)
    );
    ALTER TABLE voice_sessions ENABLE ROW LEVEL SECURITY;
    ALTER TABLE voice_sessions FORCE ROW LEVEL SECURITY;
    CREATE POLICY tenant_scope ON voice_sessions
      USING (tenant_id=NULLIF(current_setting('app.tenant_id',true),'')::uuid)
      WITH CHECK (tenant_id=NULLIF(current_setting('app.tenant_id',true),'')::uuid);
    GRANT SELECT,INSERT,UPDATE,DELETE ON voice_sessions TO scheduler_app;
    """)


def downgrade():
    op.execute("DROP TABLE voice_sessions")
    op.execute("ALTER TABLE agent_sessions DROP COLUMN transport_mode")
