"""Tenant-owned knowledge and narrow worker lease authority."""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE EXTENSION IF NOT EXISTS vector;
    DO $$ BEGIN
      IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='knowledge_worker') THEN
        CREATE ROLE knowledge_worker NOLOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT;
      END IF;
    END $$;
    CREATE TABLE knowledge_sources (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants ON DELETE CASCADE,
      agent_id uuid NOT NULL, kind text NOT NULL CHECK(kind IN ('text','faq','url','file')),
      name text NOT NULL CHECK(length(btrim(name)) BETWEEN 1 AND 200),
      revision integer NOT NULL DEFAULT 1 CHECK(revision>0), requested_revision integer NOT NULL DEFAULT 1,
      status text NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','processing','ready','failed','deleted')),
      content jsonb NOT NULL DEFAULT '{}', blob_key text, checksum text,
      active_generation_id uuid, deleted_at timestamptz, error_code text, error_at timestamptz,
      created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE(tenant_id,id), UNIQUE(tenant_id,agent_id,id),
      FOREIGN KEY(tenant_id,agent_id) REFERENCES agents(tenant_id,id),
      CHECK(requested_revision=revision), CHECK(checksum IS NULL OR checksum ~ '^[0-9a-f]{64}$'),
      CHECK((status='deleted')=(deleted_at IS NOT NULL))
    );
    CREATE FUNCTION knowledge_source_identity() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
    BEGIN
      IF (NEW.id,NEW.tenant_id,NEW.agent_id,NEW.kind) IS DISTINCT FROM (OLD.id,OLD.tenant_id,OLD.agent_id,OLD.kind)
      THEN RAISE EXCEPTION 'Source identity is immutable' USING ERRCODE='23514'; END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER knowledge_source_identity BEFORE UPDATE ON knowledge_sources
      FOR EACH ROW EXECUTE FUNCTION knowledge_source_identity();
    CREATE TABLE knowledge_generations (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, agent_id uuid NOT NULL, source_id uuid NOT NULL,
      source_revision integer NOT NULL CHECK(source_revision>0), profile text NOT NULL CHECK(length(profile)=64),
      chunk_count integer NOT NULL CHECK(chunk_count BETWEEN 1 AND 256), sealed_at timestamptz, created_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE(tenant_id,source_id,id), UNIQUE(tenant_id,agent_id,source_id,id),
      FOREIGN KEY(tenant_id,agent_id,source_id) REFERENCES knowledge_sources(tenant_id,agent_id,id) ON DELETE CASCADE
    );
    ALTER TABLE knowledge_sources ADD CONSTRAINT knowledge_active_owner
      FOREIGN KEY(tenant_id,id,active_generation_id) REFERENCES knowledge_generations(tenant_id,source_id,id);
    CREATE TABLE knowledge_chunks (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, agent_id uuid NOT NULL,
      source_id uuid NOT NULL, generation_id uuid NOT NULL, ordinal integer NOT NULL CHECK(ordinal BETWEEN 0 AND 255),
      heading text NOT NULL DEFAULT '', language text NOT NULL, text text NOT NULL CHECK(length(text) BETWEEN 1 AND 1000000),
      token_count integer NOT NULL CHECK(token_count BETWEEN 1 AND 512), embedding vector(1024) NOT NULL,
      search tsvector GENERATED ALWAYS AS (to_tsvector('simple',text)) STORED,
      UNIQUE(generation_id,ordinal),
      FOREIGN KEY(tenant_id,agent_id,source_id,generation_id) REFERENCES knowledge_generations(tenant_id,agent_id,source_id,id) ON DELETE CASCADE
    );
    CREATE INDEX knowledge_chunks_dense ON knowledge_chunks USING hnsw(embedding vector_cosine_ops);
    CREATE INDEX knowledge_chunks_keyword ON knowledge_chunks USING gin(search);
    CREATE INDEX knowledge_chunks_scope ON knowledge_chunks(tenant_id,agent_id,generation_id);
    CREATE TABLE knowledge_jobs (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, agent_id uuid NOT NULL, source_id uuid NOT NULL,
      source_revision integer NOT NULL CHECK(source_revision>0),
      status text NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','processing','ready','failed','cancelled')),
      attempts integer NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 3), lease_token uuid, lease_until timestamptz,
      available_at timestamptz NOT NULL DEFAULT now(), created_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE(source_id,source_revision),
      FOREIGN KEY(tenant_id,agent_id,source_id) REFERENCES knowledge_sources(tenant_id,agent_id,id) ON DELETE CASCADE
    );
    CREATE INDEX knowledge_jobs_due ON knowledge_jobs(status,available_at,lease_until);
    CREATE TABLE knowledge_query_events (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, agent_id uuid NOT NULL,
      command_id uuid NOT NULL, query_hash text NOT NULL CHECK(length(query_hash)=64), language text NOT NULL,
      purpose text NOT NULL, decision text NOT NULL, redacted_question varchar(1000), refs jsonb NOT NULL DEFAULT '[]', duration_ms integer NOT NULL CHECK(duration_ms>=0),
      created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(tenant_id,agent_id,command_id),
      FOREIGN KEY(tenant_id,agent_id) REFERENCES agents(tenant_id,id)
    );
    """)
    for table in (
        "knowledge_sources",
        "knowledge_generations",
        "knowledge_chunks",
        "knowledge_jobs",
        "knowledge_query_events",
    ):
        op.execute(f"""
        ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
        ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_scope ON {table}
          USING (tenant_id=NULLIF(current_setting('app.tenant_id',true),'')::uuid)
          WITH CHECK (tenant_id=NULLIF(current_setting('app.tenant_id',true),'')::uuid);
        REVOKE ALL ON {table} FROM PUBLIC;
        """)
    op.execute("""
    GRANT SELECT,INSERT,UPDATE ON knowledge_sources,knowledge_jobs TO scheduler_app;
    GRANT SELECT ON knowledge_generations,knowledge_chunks TO scheduler_app;
    GRANT SELECT,INSERT ON knowledge_query_events TO scheduler_app;
    GRANT USAGE ON SCHEMA public TO knowledge_worker;
    GRANT SELECT ON knowledge_sources,knowledge_jobs,knowledge_generations,knowledge_chunks TO knowledge_worker;
    GRANT INSERT(id,tenant_id,agent_id,source_id,source_revision,profile,chunk_count) ON knowledge_generations TO knowledge_worker;
    GRANT INSERT ON knowledge_chunks TO knowledge_worker;
    GRANT UPDATE(status,error_code,error_at,updated_at) ON knowledge_sources TO knowledge_worker;
    GRANT UPDATE ON knowledge_jobs TO knowledge_worker;
    CREATE FUNCTION knowledge_discover_jobs() RETURNS TABLE(tenant_id uuid,agent_id uuid,source_id uuid,job_id uuid)
    LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      SELECT j.tenant_id,j.agent_id,j.source_id,j.id FROM public.knowledge_jobs j
      JOIN public.knowledge_sources s ON (s.tenant_id,s.id)=(j.tenant_id,j.source_id)
      WHERE s.deleted_at IS NULL AND s.revision=j.source_revision
      AND ((j.status='queued' AND j.attempts<3 AND j.available_at<=now()) OR (j.status='processing' AND j.lease_until<now()))
      ORDER BY j.available_at,j.id LIMIT 50
    $$;
    REVOKE ALL ON FUNCTION knowledge_discover_jobs() FROM PUBLIC,scheduler_app;
    GRANT EXECUTE ON FUNCTION knowledge_discover_jobs() TO knowledge_worker;
    CREATE FUNCTION knowledge_chunk_unsealed() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
    DECLARE g public.knowledge_generations;
    BEGIN
      SELECT * INTO g FROM public.knowledge_generations
      WHERE id=NEW.generation_id AND tenant_id=NEW.tenant_id AND source_id=NEW.source_id AND agent_id=NEW.agent_id
        AND tenant_id=NULLIF(current_setting('app.tenant_id',true),'')::uuid FOR SHARE;
      IF NOT FOUND OR g.sealed_at IS NOT NULL THEN
        RAISE EXCEPTION 'Generation is not writable' USING ERRCODE='23514';
      END IF;
      RETURN NEW;
    END $$;
    REVOKE ALL ON FUNCTION knowledge_chunk_unsealed() FROM PUBLIC,scheduler_app,knowledge_worker;
    CREATE TRIGGER knowledge_chunk_unsealed BEFORE INSERT ON knowledge_chunks
      FOR EACH ROW EXECUTE FUNCTION knowledge_chunk_unsealed();

    CREATE FUNCTION knowledge_activate_generation(p_source uuid,p_job uuid,p_revision integer,p_token uuid,p_generation uuid,p_profile text)
    RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
    DECLARE tenant uuid := NULLIF(current_setting('app.tenant_id',true),'')::uuid;
      s public.knowledge_sources; j public.knowledge_jobs; g public.knowledge_generations;
      total integer; first_ordinal integer; last_ordinal integer;
    BEGIN
      -- Authority is checked after locks, with database wall time rather than transaction start.
      SELECT * INTO s FROM public.knowledge_sources WHERE id=p_source AND tenant_id=tenant FOR UPDATE;
      IF NOT FOUND OR s.deleted_at IS NOT NULL OR s.revision<>p_revision THEN RETURN false; END IF;
      SELECT * INTO j FROM public.knowledge_jobs WHERE id=p_job AND tenant_id=tenant AND source_id=p_source FOR UPDATE;
      IF NOT FOUND OR j.source_revision<>p_revision OR j.status<>'processing' OR j.lease_token IS DISTINCT FROM p_token
        OR j.lease_until IS NULL OR j.lease_until<=clock_timestamp() THEN RETURN false; END IF;
      SELECT * INTO g FROM public.knowledge_generations
        WHERE id=p_generation AND tenant_id=tenant AND source_id=p_source AND agent_id=s.agent_id FOR UPDATE;
      IF NOT FOUND OR g.sealed_at IS NOT NULL OR g.source_revision<>p_revision OR g.profile<>p_profile
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
    op.execute("""
    DROP FUNCTION IF EXISTS knowledge_activate_generation(uuid,uuid,integer,uuid,uuid,text);
    DROP FUNCTION knowledge_discover_jobs();
    ALTER TABLE knowledge_sources DROP CONSTRAINT knowledge_active_owner;
    DROP TABLE knowledge_query_events,knowledge_chunks,knowledge_generations,knowledge_jobs,knowledge_sources;
    DROP FUNCTION IF EXISTS knowledge_source_identity();
    DROP FUNCTION IF EXISTS knowledge_chunk_unsealed();
    """)
