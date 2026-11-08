"""Reject absent activation authority without rewriting applied knowledge history."""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE OR REPLACE FUNCTION knowledge_activate_generation(p_source uuid,p_job uuid,p_revision integer,p_token uuid,p_generation uuid,p_profile text)
    RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
    DECLARE tenant uuid := NULLIF(current_setting('app.tenant_id',true),'')::uuid;
      s public.knowledge_sources; j public.knowledge_jobs; g public.knowledge_generations;
      total integer; first_ordinal integer; last_ordinal integer;
    BEGIN
      IF tenant IS NULL OR p_source IS NULL OR p_job IS NULL OR p_revision IS NULL OR p_revision<1
        OR p_token IS NULL OR p_generation IS NULL OR p_profile IS NULL THEN RETURN false; END IF;
      -- Authority is checked after locks, with database wall time rather than transaction start.
      SELECT * INTO s FROM public.knowledge_sources WHERE id=p_source AND tenant_id=tenant FOR UPDATE;
      IF NOT FOUND OR s.deleted_at IS NOT NULL OR s.revision IS DISTINCT FROM p_revision THEN RETURN false; END IF;
      SELECT * INTO j FROM public.knowledge_jobs WHERE id=p_job AND tenant_id=tenant AND source_id=p_source FOR UPDATE;
      IF NOT FOUND OR j.source_revision IS DISTINCT FROM p_revision OR j.status<>'processing' OR j.lease_token IS DISTINCT FROM p_token
        OR j.lease_until IS NULL OR j.lease_until<=clock_timestamp() THEN RETURN false; END IF;
      SELECT * INTO g FROM public.knowledge_generations
        WHERE id=p_generation AND tenant_id=tenant AND source_id=p_source AND agent_id=s.agent_id FOR UPDATE;
      IF NOT FOUND OR g.sealed_at IS NOT NULL OR g.source_revision IS DISTINCT FROM p_revision OR g.profile IS DISTINCT FROM p_profile
        OR p_profile IS DISTINCT FROM '9b5d69d631ec589b88cde44c55127741cab1ec9a8e97f44dd214053f1416546c'
        THEN RETURN false; END IF;
      SELECT count(*),min(ordinal),max(ordinal) INTO total,first_ordinal,last_ordinal
        FROM public.knowledge_chunks WHERE generation_id=p_generation AND tenant_id=tenant AND source_id=p_source;
      IF total<>g.chunk_count OR total NOT BETWEEN 1 AND 256 OR first_ordinal<>0 OR last_ordinal<>total-1
        OR j.lease_until<=clock_timestamp() THEN RETURN false; END IF;
      UPDATE public.knowledge_generations SET sealed_at=clock_timestamp() WHERE id=p_generation AND tenant_id=tenant;
      UPDATE public.knowledge_sources SET active_generation_id=p_generation,status='ready',error_code=NULL,error_at=NULL,updated_at=clock_timestamp()
        WHERE id=p_source AND tenant_id=tenant;
      UPDATE public.knowledge_jobs SET status='ready',lease_token=NULL,lease_until=NULL WHERE id=p_job AND tenant_id=tenant;
      RETURN true;
    END $$;
    REVOKE ALL ON FUNCTION knowledge_activate_generation(uuid,uuid,integer,uuid,uuid,text) FROM PUBLIC,scheduler_app;
    GRANT EXECUTE ON FUNCTION knowledge_activate_generation(uuid,uuid,integer,uuid,uuid,text) TO knowledge_worker;
    """)


def downgrade():
    # This security-only replacement remains compatible with 0007 callers.
    # Keep its safe guard on downgrade; 0007's downgrade removes the whole function.
    pass
