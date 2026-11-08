"""Bounded analytics routing, chronology, worker freshness and alert deduplication."""

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    DO $$ BEGIN
      IF EXISTS(SELECT 1 FROM analytics_turns) THEN
        RAISE EXCEPTION 'analytics_chronology_requires_empty_capture_history';
      END IF;
    END $$;
    ALTER TABLE analytics_turns ADD COLUMN ordinal bigint GENERATED ALWAYS AS IDENTITY;
    CREATE UNIQUE INDEX analytics_turn_order ON analytics_turns(tenant_id,session_id,ordinal);
    GRANT USAGE ON SEQUENCE analytics_turns_ordinal_seq TO scheduler_app;
    ALTER TABLE analytics_alerts ADD COLUMN language text NOT NULL DEFAULT 'unknown'
      CHECK(language IN ('en','ur','mixed','unknown'));
    ALTER TABLE analytics_alerts ALTER COLUMN channel DROP NOT NULL;
    CREATE UNIQUE INDEX analytics_one_open_alert ON analytics_alerts(tenant_id,agent_id,channel,kind) NULLS NOT DISTINCT
      WHERE state='open';
    CREATE TABLE analytics_worker_state (
      tenant_id uuid PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
      last_success_at timestamptz NOT NULL
    );
    ALTER TABLE analytics_worker_state ENABLE ROW LEVEL SECURITY;
    ALTER TABLE analytics_worker_state FORCE ROW LEVEL SECURITY;
    CREATE POLICY tenant_scope ON analytics_worker_state
      USING(tenant_id=NULLIF(current_setting('app.tenant_id',true),'')::uuid)
      WITH CHECK(tenant_id=NULLIF(current_setting('app.tenant_id',true),'')::uuid);
    REVOKE ALL ON analytics_worker_state FROM PUBLIC;
    GRANT SELECT ON analytics_worker_state TO scheduler_app;
    GRANT SELECT,INSERT,UPDATE,DELETE ON analytics_worker_state TO analytics_worker;
    CREATE OR REPLACE FUNCTION analytics_discover_tenants(after_tenant uuid DEFAULT NULL,batch_size integer DEFAULT 50)
      RETURNS TABLE(tenant_id uuid) LANGUAGE sql SECURITY DEFINER
      SET search_path=pg_catalog,public SET statement_timeout='5s' SET lock_timeout='1s' AS $$
      SELECT candidate.tenant_id FROM (
        SELECT c.tenant_id FROM public.analytics_calls c
        UNION SELECT d.tenant_id FROM public.analytics_dirty_buckets d
        UNION SELECT s.tenant_id FROM public.analytics_settings s
        UNION SELECT a.tenant_id FROM public.analytics_alerts a
        UNION SELECT h.tenant_id FROM public.analytics_hourly_rollups h
        UNION SELECT r.tenant_id FROM public.analytics_daily_rollups r
        UNION SELECT w.tenant_id FROM public.analytics_worker_state w
        UNION SELECT j.tenant_id FROM public.knowledge_jobs j WHERE j.status='queued'
      ) candidate WHERE after_tenant IS NULL OR candidate.tenant_id>after_tenant
      ORDER BY candidate.tenant_id LIMIT LEAST(GREATEST(COALESCE(batch_size,50),1),50)
    $$;
    CREATE FUNCTION analytics_agent_backlog(as_of timestamptz,after_agent uuid DEFAULT NULL,batch_size integer DEFAULT 100)
      RETURNS TABLE(agent_id uuid,queued_count bigint,oldest_age_seconds double precision)
      LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,public
      SET statement_timeout='5s' SET lock_timeout='1s' AS $$
      SELECT j.agent_id,count(*),GREATEST(0,extract(epoch FROM as_of-min(j.created_at)))::double precision
      FROM public.knowledge_jobs j WHERE j.tenant_id=NULLIF(current_setting('app.tenant_id',true),'')::uuid
        AND j.status='queued' AND (after_agent IS NULL OR j.agent_id>after_agent)
      GROUP BY j.agent_id ORDER BY j.agent_id LIMIT LEAST(GREATEST(COALESCE(batch_size,100),1),100)
    $$;
    REVOKE ALL ON FUNCTION analytics_agent_backlog(timestamptz,uuid,integer) FROM PUBLIC,scheduler_app,knowledge_worker;
    GRANT EXECUTE ON FUNCTION analytics_agent_backlog(timestamptz,uuid,integer) TO analytics_worker;
    """)


def downgrade():
    op.execute("""
    DROP FUNCTION analytics_agent_backlog(timestamptz,uuid,integer);
    CREATE OR REPLACE FUNCTION analytics_discover_tenants(after_tenant uuid DEFAULT NULL,batch_size integer DEFAULT 50)
      RETURNS TABLE(tenant_id uuid) LANGUAGE sql SECURITY DEFINER
      SET search_path=pg_catalog,public SET statement_timeout='5s' SET lock_timeout='1s' AS $$
      SELECT candidate.tenant_id FROM (
        SELECT c.tenant_id FROM public.analytics_calls c UNION SELECT d.tenant_id FROM public.analytics_dirty_buckets d
        UNION SELECT s.tenant_id FROM public.analytics_settings s UNION SELECT a.tenant_id FROM public.analytics_alerts a
        UNION SELECT h.tenant_id FROM public.analytics_hourly_rollups h UNION SELECT r.tenant_id FROM public.analytics_daily_rollups r
      ) candidate WHERE after_tenant IS NULL OR candidate.tenant_id>after_tenant
      ORDER BY candidate.tenant_id LIMIT LEAST(GREATEST(COALESCE(batch_size,50),1),50)
    $$;
    DROP TABLE analytics_worker_state;
    DROP INDEX analytics_one_open_alert;
    DELETE FROM analytics_alerts WHERE channel IS NULL;
    ALTER TABLE analytics_alerts ALTER COLUMN channel SET NOT NULL;
    ALTER TABLE analytics_alerts DROP COLUMN language;
    ALTER TABLE analytics_turns DROP COLUMN ordinal;
    """)
