"""Analytics-only lifecycle ledger, retained aggregates and narrow worker authority."""

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

TABLES = (
    "analytics_settings",
    "analytics_calls",
    "analytics_tasks",
    "analytics_operations",
    "analytics_turns",
    "analytics_transcripts",
    "analytics_stage_metrics",
    "analytics_usage_invocations",
    "analytics_hourly_rollups",
    "analytics_daily_rollups",
    "analytics_alerts",
    "analytics_dirty_buckets",
)

COUNTERS = (
    "sessions",
    "ended_sessions",
    "booking_sessions",
    "handoff_sessions",
    "abandoned_sessions",
    "book_operations",
    "cancel_operations",
    "reschedule_operations",
    "attempted_tasks",
    "completed_tasks",
    "caller_turns",
    "tool_results",
    "tool_failures",
    "prompt_emissions",
    "repeated_prompts",
    "ended_duration_samples",
    "usage_invocations",
    "usage_known",
    "cost_known",
    "input_tokens",
    "output_tokens",
)


def upgrade():
    op.execute("""
    DO $$ BEGIN
      IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='analytics_worker') THEN
        CREATE ROLE analytics_worker NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
          NOINHERIT NOREPLICATION NOBYPASSRLS;
      END IF;
    END $$;
    ALTER ROLE analytics_worker NOSUPERUSER NOCREATEDB NOCREATEROLE
      NOINHERIT NOREPLICATION NOBYPASSRLS;

    CREATE FUNCTION analytics_safe_metadata(value jsonb) RETURNS boolean
      LANGUAGE plpgsql IMMUTABLE SET search_path=pg_catalog AS $$
    DECLARE k text; v jsonb;
    BEGIN
      IF jsonb_typeof(value)<>'object' OR octet_length(value::text)>2048 THEN RETURN false; END IF;
      FOR k,v IN SELECT * FROM jsonb_each(value) LOOP
        IF k IN ('progressed','repeated_prompt','accepted','trusted','cached','filler','cache_hit','uncertain') THEN
          IF jsonb_typeof(v)<>'boolean' THEN RETURN false; END IF;
        ELSIF k='stt_confidence' THEN
          IF jsonb_typeof(v)<>'number' THEN RETURN false; END IF;
          IF (v::text)::numeric NOT BETWEEN 0 AND 1 THEN RETURN false; END IF;
        ELSIF k='prompt_id' THEN
          IF v #>> '{}' NOT IN ('ask_date','ask_need','ask_patient','ask_verification',
            'ask_slot','ask_appointment','readback','consent','consent_clarification',
            'info','greeting','handoff','end','other') OR jsonb_typeof(v)<>'string'
          THEN RETURN false; END IF;
        ELSIF k='delivery' THEN
          IF v #>> '{}' NOT IN ('generated','delivered','interrupted') OR jsonb_typeof(v)<>'string'
          THEN RETURN false; END IF;
        ELSIF k='output_state' THEN
          IF v #>> '{}' NOT IN ('generated','queued','delivered','interrupted') OR jsonb_typeof(v)<>'string'
          THEN RETURN false; END IF;
        ELSIF k='error_code' THEN
          IF v #>> '{}' NOT IN ('provider_timeout','provider_unavailable','provider_malformed',
            'tool_failed','validation_failed','conflict','not_found','unauthorized')
            OR jsonb_typeof(v)<>'string' THEN RETURN false; END IF;
        ELSE RETURN false;
        END IF;
      END LOOP;
      RETURN true;
    END $$;
    CREATE FUNCTION analytics_uuid_stack(value jsonb) RETURNS boolean
      LANGUAGE plpgsql IMMUTABLE SET search_path=pg_catalog AS $$
    DECLARE item jsonb;
    BEGIN
      IF jsonb_typeof(value)<>'array' THEN RETURN false; END IF;
      IF jsonb_array_length(value)>8 THEN RETURN false; END IF;
      FOR item IN SELECT * FROM jsonb_array_elements(value) LOOP
        IF jsonb_typeof(item)<>'string' OR (item #>> '{}') !~
          '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        THEN RETURN false; END IF;
      END LOOP;
      RETURN true;
    END $$;
    CREATE FUNCTION analytics_flags(value jsonb) RETURNS boolean
      LANGUAGE plpgsql IMMUTABLE SET search_path=pg_catalog AS $$
    DECLARE item jsonb;
    BEGIN
      IF jsonb_typeof(value)<>'array' THEN RETURN false; END IF;
      IF jsonb_array_length(value)>4 THEN RETURN false; END IF;
      FOR item IN SELECT * FROM jsonb_array_elements(value) LOOP
        IF jsonb_typeof(item)<>'string' OR item #>> '{}' NOT IN
          ('repeated_prompt','interruptions','low_stt_confidence','handoff') THEN RETURN false; END IF;
      END LOOP;
      RETURN true;
    END $$;
    CREATE TABLE analytics_settings (
      tenant_id uuid PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
      revision integer NOT NULL DEFAULT 1 CHECK(revision>0),
      capture_enabled boolean NOT NULL DEFAULT false,
      retention_days integer NOT NULL DEFAULT 7 CHECK(retention_days BETWEEN 1 AND 30),
      updated_at timestamptz NOT NULL DEFAULT now()
    );
    ALTER TABLE agent_sessions ADD CONSTRAINT analytics_session_agent_owner UNIQUE(tenant_id,agent_id,id);
    CREATE TABLE analytics_calls (
      session_id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
      agent_id uuid NOT NULL, trace_id uuid NOT NULL UNIQUE,
      purpose text NOT NULL CHECK(purpose IN ('production','test')),
      channel text NOT NULL CHECK(channel IN ('text','voice')),
      timezone text NOT NULL CHECK(length(timezone) BETWEEN 1 AND 100),
      local_date date NOT NULL, utc_offset_minutes integer NOT NULL CHECK(utc_offset_minutes BETWEEN -840 AND 840),
      started_at timestamptz NOT NULL, expires_at timestamptz NOT NULL,
      ended_at timestamptz, updated_at timestamptz NOT NULL DEFAULT now(),
      status text NOT NULL DEFAULT 'active' CHECK(status IN ('active','disconnected','ended','expired','failed')),
      outcome text CHECK(outcome IN ('booked','cancelled','rescheduled','info_only','abandoned','handoff','error')),
      end_reason text CHECK(end_reason IN ('caller_end','staff_end','handoff','timeout','error','provisioning_failed','disconnect')),
      language text NOT NULL DEFAULT 'unknown' CHECK(language IN ('en','ur','mixed','unknown')),
      capture_enabled_at_start boolean NOT NULL DEFAULT false,
      consent text NOT NULL DEFAULT 'off' CHECK(consent IN ('off','pending','accepted','declined')),
      consent_at timestamptz, transcript_deleted_at timestamptz,
      active_task_id uuid, parked_task_ids jsonb NOT NULL DEFAULT '[]' CHECK(analytics_uuid_stack(parked_task_ids)),
      quality_flags jsonb NOT NULL DEFAULT '[]' CHECK(analytics_flags(quality_flags)),
      summary_revision integer NOT NULL DEFAULT 0 CHECK(summary_revision>=0),
      summarized_at timestamptz,
      UNIQUE(tenant_id,session_id), UNIQUE(tenant_id,agent_id,session_id),
      FOREIGN KEY(tenant_id,agent_id,session_id) REFERENCES agent_sessions(tenant_id,agent_id,id),
      CHECK(expires_at>=started_at), CHECK(ended_at IS NULL OR ended_at>=started_at),
      CHECK(capture_enabled_at_start OR consent='off')
    );
    CREATE INDEX analytics_calls_cohort ON analytics_calls(tenant_id,purpose,local_date,agent_id,channel,session_id);
    CREATE INDEX analytics_calls_expiry ON analytics_calls(tenant_id,expires_at,session_id) WHERE ended_at IS NULL;
    CREATE INDEX analytics_calls_retention ON analytics_calls(tenant_id,started_at,session_id);
    CREATE TABLE analytics_tasks (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, session_id uuid NOT NULL,
      kind text NOT NULL CHECK(kind IN ('book','cancel','reschedule')),
      status text NOT NULL DEFAULT 'active' CHECK(status IN ('active','parked','completed','abandoned')),
      started_at timestamptz NOT NULL, ended_at timestamptz,
      caller_turns integer NOT NULL DEFAULT 0 CHECK(caller_turns>=0),
      UNIQUE(tenant_id,session_id,id),
      FOREIGN KEY(tenant_id,session_id) REFERENCES analytics_calls(tenant_id,session_id) ON DELETE CASCADE,
      CHECK(ended_at IS NULL OR ended_at>=started_at)
    );
    CREATE INDEX analytics_tasks_session ON analytics_tasks(tenant_id,session_id,started_at,id);
    ALTER TABLE analytics_calls ADD CONSTRAINT analytics_active_task_owner
      FOREIGN KEY(tenant_id,session_id,active_task_id) REFERENCES analytics_tasks(tenant_id,session_id,id)
      DEFERRABLE INITIALLY DEFERRED;
    CREATE TABLE analytics_operations (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, session_id uuid NOT NULL,
      event_id uuid NOT NULL, task_id uuid, operation text NOT NULL CHECK(operation IN ('book','cancel','reschedule')),
      occurred_at timestamptz NOT NULL,
      UNIQUE(tenant_id,session_id,event_id),
      FOREIGN KEY(tenant_id,session_id) REFERENCES analytics_calls(tenant_id,session_id) ON DELETE CASCADE,
      FOREIGN KEY(tenant_id,session_id,task_id) REFERENCES analytics_tasks(tenant_id,session_id,id)
    );
    CREATE INDEX analytics_operations_session ON analytics_operations(tenant_id,session_id,occurred_at,id);
    CREATE TABLE analytics_turns (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, session_id uuid NOT NULL,
      event_id uuid NOT NULL, command_id uuid, task_id uuid,
      kind text NOT NULL CHECK(kind IN ('session.started','turn.user','turn.assistant','tool.called',
        'tool.result','metric.turn','lang.switched','handoff.requested','session.ended')),
      language text NOT NULL CHECK(language IN ('en','ur','mixed','unknown')),
      tool_name text CHECK(tool_name IN ('update_dialogue_state','find_patient','set_caller_contact',
        'verify_patient','prepare_patient','create_patient','get_clinic_info','search_knowledge',
        'get_available_slots','hold_slot','find_appointments','select_appointment','read_back',
        'book_appointment','cancel_appointment','reschedule_appointment','confirm','ask_caller',
        'resume_task','request_handoff','end_call')),
      tool_status text CHECK(tool_status IN ('success','failure')),
      safe_metadata jsonb NOT NULL DEFAULT '{}' CHECK(analytics_safe_metadata(safe_metadata)),
      occurred_at timestamptz NOT NULL,
      UNIQUE(tenant_id,session_id,event_id),
      FOREIGN KEY(tenant_id,session_id) REFERENCES analytics_calls(tenant_id,session_id) ON DELETE CASCADE,
      FOREIGN KEY(tenant_id,session_id,task_id) REFERENCES analytics_tasks(tenant_id,session_id,id)
    );
    CREATE INDEX analytics_turns_session ON analytics_turns(tenant_id,session_id,occurred_at,id);
    CREATE TABLE analytics_transcripts (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, session_id uuid NOT NULL,
      event_id uuid NOT NULL, speaker text NOT NULL CHECK(speaker IN ('user','assistant')),
      text text NOT NULL CHECK(length(text) BETWEEN 1 AND 8000),
      occurred_at timestamptz NOT NULL, expires_at timestamptz NOT NULL CHECK(expires_at>=occurred_at),
      UNIQUE(tenant_id,session_id,event_id),
      FOREIGN KEY(tenant_id,session_id,event_id) REFERENCES analytics_turns(tenant_id,session_id,event_id) ON DELETE CASCADE
    );
    CREATE INDEX analytics_transcripts_expiry ON analytics_transcripts(tenant_id,expires_at,id);
    CREATE TABLE analytics_stage_metrics (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, session_id uuid NOT NULL,
      event_id uuid NOT NULL, turn_event_id uuid,
      stage text NOT NULL CHECK(stage IN ('command_total','tool','llm_completion','llm_first_token',
        'stt_completion','tts_first_byte','end_of_speech_to_audio','tts_buffered_generation',
        'audio_queue','end_of_speech_to_audio_queue')),
      language text NOT NULL CHECK(language IN ('en','ur','mixed','unknown')),
      origin text NOT NULL CHECK(origin IN ('server','native')),
      duration_ms double precision NOT NULL CHECK(duration_ms BETWEEN 0 AND 600000),
      observed_at timestamptz NOT NULL,
      UNIQUE(tenant_id,session_id,event_id),
      FOREIGN KEY(tenant_id,session_id) REFERENCES analytics_calls(tenant_id,session_id) ON DELETE CASCADE,
      FOREIGN KEY(tenant_id,session_id,turn_event_id) REFERENCES analytics_turns(tenant_id,session_id,event_id)
    );
    CREATE INDEX analytics_stage_samples ON analytics_stage_metrics(tenant_id,observed_at,stage,session_id);
    CREATE TABLE analytics_usage_invocations (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, session_id uuid NOT NULL,
      invocation_id uuid NOT NULL UNIQUE,
      provider text NOT NULL CHECK(provider IN ('nim')),
      model varchar(128) NOT NULL CHECK(model ~ '^[A-Za-z0-9_./:-]{1,128}$'),
      status text NOT NULL CHECK(status IN ('started','success','malformed','timeout','failure')),
      started_at timestamptz NOT NULL, completed_at timestamptz,
      input_tokens bigint CHECK(input_tokens BETWEEN 0 AND 1000000000000),
      output_tokens bigint CHECK(output_tokens BETWEEN 0 AND 1000000000000),
      input_rate numeric(24,12) CHECK(input_rate BETWEEN 0 AND 1000000),
      output_rate numeric(24,12) CHECK(output_rate BETWEEN 0 AND 1000000),
      cost numeric(30,12) CHECK(cost BETWEEN 0 AND 1000000000000),
      CHECK(completed_at IS NULL OR completed_at>=started_at),
      CHECK(cost IS NULL OR (input_tokens IS NOT NULL AND output_tokens IS NOT NULL
        AND input_rate IS NOT NULL AND output_rate IS NOT NULL)),
      FOREIGN KEY(tenant_id,session_id) REFERENCES analytics_calls(tenant_id,session_id) ON DELETE CASCADE
    );
    CREATE INDEX analytics_usage_session ON analytics_usage_invocations(tenant_id,session_id,started_at,invocation_id);
    """)
    counters = ",".join(f"{name} bigint NOT NULL DEFAULT 0 CHECK({name}>=0)" for name in COUNTERS)
    for table, hour in (("analytics_hourly_rollups", True), ("analytics_daily_rollups", False)):
        hourly_columns = (
            "bucket_start timestamptz NOT NULL,utc_offset_minutes integer NOT NULL CHECK(utc_offset_minutes BETWEEN -840 AND 840),"
            if hour
            else ""
        )
        key = "bucket_start," if hour else ""
        op.execute(f"""
        CREATE TABLE {table} (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
          agent_id uuid NOT NULL, purpose text NOT NULL CHECK(purpose IN ('production','test')),
          channel text NOT NULL CHECK(channel IN ('text','voice')),
          timezone text NOT NULL CHECK(length(timezone) BETWEEN 1 AND 100),local_date date NOT NULL,
          {hourly_columns}{counters},
          ended_duration_ms numeric(30,3) NOT NULL DEFAULT 0 CHECK(ended_duration_ms BETWEEN 0 AND 1000000000000000000),
          known_cost numeric(30,12) NOT NULL DEFAULT 0 CHECK(known_cost BETWEEN 0 AND 1000000000000000000),
          as_of timestamptz NOT NULL, sealed_at timestamptz,
          UNIQUE(tenant_id,agent_id,purpose,channel,timezone,local_date{"," + key.rstrip(",") if hour else ""}),
          FOREIGN KEY(tenant_id,agent_id) REFERENCES agents(tenant_id,id)
        );
        CREATE INDEX {table}_retention ON {table}(tenant_id,local_date,id);
        """)
    op.execute("""
    CREATE TABLE analytics_dirty_buckets (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
      agent_id uuid NOT NULL,purpose text NOT NULL CHECK(purpose IN ('production','test')),
      channel text NOT NULL CHECK(channel IN ('text','voice')),timezone text NOT NULL CHECK(length(timezone) BETWEEN 1 AND 100),
      local_date date NOT NULL,bucket_start timestamptz NOT NULL,
      utc_offset_minutes integer NOT NULL CHECK(utc_offset_minutes BETWEEN -840 AND 840),
      revision bigint NOT NULL DEFAULT 1 CHECK(revision>0),
      dirty_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE(tenant_id,agent_id,purpose,channel,timezone,local_date,bucket_start),
      FOREIGN KEY(tenant_id,agent_id) REFERENCES agents(tenant_id,id)
    );
    CREATE INDEX analytics_dirty_due ON analytics_dirty_buckets(tenant_id,dirty_at,id);
    CREATE TABLE analytics_alerts (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
      agent_id uuid NOT NULL,channel text NOT NULL CHECK(channel IN ('text','voice')),
      kind text NOT NULL CHECK(kind IN ('latency','tool_failure','provider_failure','ended_error','backlog')),
      state text NOT NULL CHECK(state IN ('open','resolved')),
      window_start timestamptz NOT NULL,window_end timestamptz NOT NULL,
      numerator bigint NOT NULL CHECK(numerator>=0),denominator bigint NOT NULL CHECK(denominator>=0),
      observed_value double precision CHECK(observed_value BETWEEN 0 AND 1000000000000),
      opened_at timestamptz NOT NULL,last_seen_at timestamptz NOT NULL,resolved_at timestamptz,
      UNIQUE(tenant_id,agent_id,channel,kind,window_start),
      FOREIGN KEY(tenant_id,agent_id) REFERENCES agents(tenant_id,id),
      CHECK(window_end=window_start+interval '15 minutes'),
      CHECK((state='resolved')=(resolved_at IS NOT NULL))
    );
    CREATE INDEX analytics_alerts_history ON analytics_alerts(tenant_id,last_seen_at,id);
    """)
    for table in TABLES:
        op.execute(f"""
        ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
        ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_scope ON {table}
          USING(tenant_id=NULLIF(current_setting('app.tenant_id',true),'')::uuid)
          WITH CHECK(tenant_id=NULLIF(current_setting('app.tenant_id',true),'')::uuid);
        REVOKE ALL ON {table} FROM PUBLIC;
        GRANT SELECT,INSERT,UPDATE,DELETE ON {table} TO scheduler_app;
        """)
        if table == "analytics_settings":
            op.execute(f"GRANT SELECT ON {table} TO analytics_worker")
        elif table in (
            "analytics_hourly_rollups",
            "analytics_daily_rollups",
            "analytics_alerts",
            "analytics_dirty_buckets",
        ):
            op.execute(f"GRANT SELECT,INSERT,UPDATE,DELETE ON {table} TO analytics_worker")
        elif table != "analytics_transcripts":
            op.execute(f"GRANT SELECT,DELETE ON {table} TO analytics_worker")
    op.execute("""
    GRANT USAGE ON SCHEMA public TO analytics_worker;
    GRANT UPDATE(status,outcome,end_reason,ended_at,updated_at,quality_flags,summary_revision,summarized_at,
      active_task_id,parked_task_ids) ON analytics_calls TO analytics_worker;
    GRANT UPDATE(status,ended_at) ON analytics_tasks TO analytics_worker;
    GRANT SELECT(id,tenant_id,session_id,event_id,speaker,occurred_at,expires_at),DELETE
      ON analytics_transcripts TO analytics_worker;
    CREATE FUNCTION analytics_discover_tenants(after_tenant uuid DEFAULT NULL,batch_size integer DEFAULT 50)
      RETURNS TABLE(tenant_id uuid) LANGUAGE sql SECURITY DEFINER
      SET search_path=pg_catalog,public SET statement_timeout='5s' SET lock_timeout='1s' AS $$
      SELECT candidate.tenant_id FROM (
        SELECT c.tenant_id FROM public.analytics_calls c
        UNION SELECT d.tenant_id FROM public.analytics_dirty_buckets d
        UNION SELECT s.tenant_id FROM public.analytics_settings s
        UNION SELECT a.tenant_id FROM public.analytics_alerts a
        UNION SELECT h.tenant_id FROM public.analytics_hourly_rollups h
        UNION SELECT r.tenant_id FROM public.analytics_daily_rollups r
      ) candidate WHERE after_tenant IS NULL OR candidate.tenant_id>after_tenant
      ORDER BY candidate.tenant_id LIMIT LEAST(GREATEST(COALESCE(batch_size,50),1),50)
    $$;
    CREATE FUNCTION analytics_backlog() RETURNS TABLE(queued_count bigint,oldest_age_seconds double precision)
      LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,public
      SET statement_timeout='5s' SET lock_timeout='1s' AS $$
      SELECT count(*),CASE WHEN count(*)=0 THEN NULL ELSE
        GREATEST(0,extract(epoch FROM clock_timestamp()-min(created_at)))::double precision END
      FROM public.knowledge_jobs WHERE tenant_id=NULLIF(current_setting('app.tenant_id',true),'')::uuid
        AND status='queued'
    $$;
    REVOKE ALL ON FUNCTION analytics_discover_tenants(uuid,integer),analytics_backlog()
      FROM PUBLIC,scheduler_app,knowledge_worker;
    GRANT EXECUTE ON FUNCTION analytics_discover_tenants(uuid,integer),analytics_backlog() TO analytics_worker;
    """)


def downgrade():
    op.execute("DROP FUNCTION analytics_discover_tenants(uuid,integer),analytics_backlog()")
    op.execute("ALTER TABLE analytics_calls DROP CONSTRAINT analytics_active_task_owner")
    for table in reversed(TABLES):
        op.execute(f"DROP TABLE {table}")
    op.execute("ALTER TABLE agent_sessions DROP CONSTRAINT analytics_session_agent_owner")
    op.execute(
        "DROP FUNCTION analytics_safe_metadata(jsonb),analytics_uuid_stack(jsonb),analytics_flags(jsonb)"
    )
    # Cluster role/optional credentials deliberately survive migration rollback.
