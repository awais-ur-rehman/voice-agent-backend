"""Parent-owned draft scopes and narrow synthetic tenant provisioning."""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE TABLE synthetic_tenants (
      tenant_id uuid PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
      parent_tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
      UNIQUE(parent_tenant_id,tenant_id)
    );
    REVOKE ALL ON synthetic_tenants FROM PUBLIC,scheduler_app;
    CREATE TABLE draft_tests (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants ON DELETE CASCADE,
      creator_id uuid NOT NULL, agent_id uuid NOT NULL, draft_revision integer NOT NULL CHECK(draft_revision>0),
      child_tenant_id uuid NOT NULL UNIQUE, child_actor_id uuid NOT NULL,
      child_agent_id uuid, session_id uuid, voice_session_id uuid,
      request_key text NOT NULL CHECK(length(request_key) BETWEEN 8 AND 100),
      status text NOT NULL DEFAULT 'active' CHECK(status IN ('active','ended','expired')),
      expires_at timestamptz NOT NULL, cleanup_done boolean NOT NULL DEFAULT false, fixtures jsonb NOT NULL DEFAULT '{}',
      UNIQUE(tenant_id,creator_id,request_key),
      FOREIGN KEY(tenant_id,creator_id) REFERENCES users(tenant_id,id),
      FOREIGN KEY(tenant_id,agent_id) REFERENCES agents(tenant_id,id),
      FOREIGN KEY(tenant_id,child_tenant_id) REFERENCES synthetic_tenants(parent_tenant_id,tenant_id),
      FOREIGN KEY(child_tenant_id,child_actor_id) REFERENCES users(tenant_id,id),
      FOREIGN KEY(child_tenant_id,child_agent_id) REFERENCES agents(tenant_id,id),
      FOREIGN KEY(child_tenant_id,session_id) REFERENCES agent_sessions(tenant_id,id),
      FOREIGN KEY(child_tenant_id,voice_session_id) REFERENCES agent_sessions(tenant_id,id)
    );
    CREATE INDEX draft_tests_expiry ON draft_tests(tenant_id,expires_at) WHERE status='active';
    ALTER TABLE draft_tests ENABLE ROW LEVEL SECURITY;
    ALTER TABLE draft_tests FORCE ROW LEVEL SECURITY;
    CREATE POLICY tenant_scope ON draft_tests
      USING (tenant_id=NULLIF(current_setting('app.tenant_id',true),'')::uuid)
      WITH CHECK (tenant_id=NULLIF(current_setting('app.tenant_id',true),'')::uuid);
    GRANT SELECT ON draft_tests TO scheduler_app;
    GRANT UPDATE(status,child_agent_id,session_id,voice_session_id,fixtures,cleanup_done) ON draft_tests TO scheduler_app;
    CREATE FUNCTION provision_draft_test(actor uuid, parent_agent uuid, saved_revision integer, retry_key text, at_time timestamptz)
      RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
    DECLARE parent uuid := NULLIF(current_setting('app.tenant_id',true),'')::uuid;
      child uuid := gen_random_uuid(); child_actor uuid := gen_random_uuid(); identity uuid := gen_random_uuid();
    BEGIN
      IF NOT EXISTS(SELECT 1 FROM public.users WHERE id=actor AND tenant_id=parent AND active AND role IN ('owner','admin'))
        OR NOT EXISTS(SELECT 1 FROM public.agents WHERE id=parent_agent AND tenant_id=parent AND enabled AND archived_at IS NULL)
        OR EXISTS(SELECT 1 FROM public.synthetic_tenants WHERE tenant_id=parent)
        OR NOT EXISTS(SELECT 1 FROM public.agent_drafts WHERE agent_id=parent_agent AND tenant_id=parent AND revision=saved_revision)
      THEN RAISE EXCEPTION 'Invalid test scope' USING ERRCODE='42501'; END IF;
      PERFORM 1 FROM public.tenants WHERE id=parent FOR UPDATE;
      IF (SELECT count(*) FROM public.draft_tests WHERE tenant_id=parent AND status='active' AND expires_at>at_time)>=3
        OR EXISTS(SELECT 1 FROM public.draft_tests WHERE tenant_id=parent AND creator_id=actor AND agent_id=parent_agent AND status='active' AND expires_at>at_time)
      THEN RAISE EXCEPTION 'Test capacity' USING ERRCODE='23514'; END IF;
      INSERT INTO public.tenants(id,name,timezone,currency,min_notice_min,horizon_days,cancellation_min,daily_limit,
        address_en,address_ur,preparation_en,preparation_ur)
      SELECT child,'Synthetic test clinic',timezone,currency,min_notice_min,horizon_days,cancellation_min,daily_limit,
        address_en,address_ur,preparation_en,preparation_ur FROM public.tenants WHERE id=parent;
      INSERT INTO public.synthetic_tenants VALUES(child,parent);
      INSERT INTO public.users(id,tenant_id,email,password_hash,role,active)
        VALUES(child_actor,child,child_actor::text || '@synthetic.invalid',gen_random_uuid()::text,'owner',false);
      INSERT INTO public.draft_tests(id,tenant_id,creator_id,agent_id,draft_revision,child_tenant_id,child_actor_id,request_key,expires_at)
        VALUES(identity,parent,actor,parent_agent,saved_revision,child,child_actor,retry_key,at_time+interval '30 minutes');
      RETURN identity;
    END $$;
    CREATE FUNCTION draft_tests_due(at_time timestamptz) RETURNS TABLE(id uuid,tenant_id uuid,creator_id uuid)
      LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      SELECT id,tenant_id,creator_id FROM public.draft_tests WHERE NOT cleanup_done
        AND (expires_at<=at_time OR status<>'active') ORDER BY expires_at,id LIMIT 50
    $$;
    REVOKE ALL ON FUNCTION draft_tests_due(timestamptz) FROM PUBLIC;
    GRANT EXECUTE ON FUNCTION draft_tests_due(timestamptz) TO scheduler_app;
    REVOKE ALL ON FUNCTION provision_draft_test(uuid,uuid,integer,text,timestamptz) FROM PUBLIC;
    GRANT EXECUTE ON FUNCTION provision_draft_test(uuid,uuid,integer,text,timestamptz) TO scheduler_app;
    """)


def downgrade():
    op.execute("DROP FUNCTION IF EXISTS draft_tests_due(timestamptz)")
    op.execute("DROP FUNCTION provision_draft_test(uuid,uuid,integer,text,timestamptz)")
    op.execute("DROP TABLE draft_tests; DROP TABLE synthetic_tenants")
