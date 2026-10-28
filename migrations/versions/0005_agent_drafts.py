"""Revisioned drafts and immutable publication audit metadata."""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    ALTER TABLE agents ADD COLUMN revision integer NOT NULL DEFAULT 1 CHECK(revision>0),
      ADD COLUMN archived_at timestamptz, ADD COLUMN archived_by uuid,
      ADD CONSTRAINT agent_archiver FOREIGN KEY(tenant_id,archived_by) REFERENCES users(tenant_id,id);
    ALTER TABLE agent_versions ADD COLUMN purpose text NOT NULL DEFAULT 'production' CHECK(purpose IN ('production','test')),
      ADD COLUMN number integer CHECK(number>0), ADD COLUMN publisher_id uuid,
      ADD COLUMN rollback_source_id uuid,
      ADD CONSTRAINT agent_publisher FOREIGN KEY(tenant_id,publisher_id) REFERENCES users(tenant_id,id),
      ADD CONSTRAINT agent_rollback_source FOREIGN KEY(tenant_id,agent_id,rollback_source_id) REFERENCES agent_versions(tenant_id,agent_id,id);
    ALTER TABLE agent_versions DISABLE TRIGGER immutable_agent_version;
    WITH numbered AS (SELECT id,row_number() OVER(PARTITION BY tenant_id,agent_id ORDER BY created_at,id) n FROM agent_versions)
    UPDATE agent_versions SET number=numbered.n FROM numbered WHERE numbered.id=agent_versions.id;
    ALTER TABLE agent_versions ENABLE TRIGGER immutable_agent_version;
    CREATE UNIQUE INDEX agent_publication_number ON agent_versions(tenant_id,agent_id,number) WHERE purpose='production';
    CREATE TABLE agent_drafts (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants,
      agent_id uuid NOT NULL, revision integer NOT NULL DEFAULT 1 CHECK(revision>0),
      config jsonb NOT NULL, updated_at timestamptz NOT NULL DEFAULT now(), updated_by uuid,
      UNIQUE(tenant_id,id), UNIQUE(tenant_id,agent_id),
      FOREIGN KEY(tenant_id,agent_id) REFERENCES agents(tenant_id,id),
      FOREIGN KEY(tenant_id,updated_by) REFERENCES users(tenant_id,id)
    );
    INSERT INTO agent_drafts(tenant_id,agent_id,config)
      SELECT a.tenant_id,a.id,coalesce(v.config,'{}'::jsonb) FROM agents a LEFT JOIN agent_versions v ON v.id=a.active_version_id;
    ALTER TABLE agent_drafts ENABLE ROW LEVEL SECURITY; ALTER TABLE agent_drafts FORCE ROW LEVEL SECURITY;
    CREATE POLICY tenant_scope ON agent_drafts USING(tenant_id=NULLIF(current_setting('app.tenant_id',true),'')::uuid)
      WITH CHECK(tenant_id=NULLIF(current_setting('app.tenant_id',true),'')::uuid);
    GRANT SELECT,INSERT,UPDATE,DELETE ON agent_drafts TO scheduler_app;
    CREATE TRIGGER agent_revision BEFORE UPDATE ON agents FOR EACH ROW EXECUTE FUNCTION dashboard_revision();
    """)


def downgrade():
    op.execute("""
    DROP TRIGGER agent_revision ON agents; DROP TABLE agent_drafts; DROP INDEX agent_publication_number;
    ALTER TABLE agent_versions DROP CONSTRAINT agent_rollback_source, DROP CONSTRAINT agent_publisher,
      DROP COLUMN purpose, DROP COLUMN number, DROP COLUMN publisher_id, DROP COLUMN rollback_source_id;
    ALTER TABLE agents DROP CONSTRAINT agent_archiver, DROP COLUMN revision, DROP COLUMN archived_at, DROP COLUMN archived_by;
    """)
