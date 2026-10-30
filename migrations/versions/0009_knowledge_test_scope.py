"""Read-only synthetic-to-original knowledge routing; draft, test DB only until Task 7."""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE FUNCTION draft_test_knowledge_scope(child_session uuid, child_agent uuid,
      child_version uuid, at_time timestamptz)
      RETURNS TABLE(test_id uuid, tenant_id uuid, agent_id uuid, creator_id uuid)
      LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      SELECT d.id,d.tenant_id,d.agent_id,d.creator_id
      FROM public.draft_tests d
      JOIN public.users u ON (u.tenant_id,u.id)=(d.tenant_id,d.creator_id)
      JOIN public.agents a ON (a.tenant_id,a.id)=(d.tenant_id,d.agent_id)
      JOIN public.agent_sessions s ON (s.tenant_id,s.agent_id,s.id,s.version_id)=
        (d.child_tenant_id,d.child_agent_id,child_session,child_version)
      WHERE d.child_tenant_id=NULLIF(current_setting('app.tenant_id',true),'')::uuid
        AND d.child_agent_id=child_agent AND child_session IN (d.session_id,d.voice_session_id)
        AND d.status='active' AND d.expires_at>at_time AND u.active AND u.role IN ('owner','admin')
        AND a.enabled AND a.archived_at IS NULL AND s.status='active' AND s.revoked_at IS NULL
        AND s.expires_at>at_time
    $$;
    REVOKE ALL ON FUNCTION draft_test_knowledge_scope(uuid,uuid,uuid,timestamptz) FROM PUBLIC;
    GRANT EXECUTE ON FUNCTION draft_test_knowledge_scope(uuid,uuid,uuid,timestamptz) TO scheduler_app;
    """)


def downgrade():
    op.execute("DROP FUNCTION draft_test_knowledge_scope(uuid,uuid,uuid,timestamptz)")
