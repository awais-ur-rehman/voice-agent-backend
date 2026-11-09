"""Narrow PostgreSQL admission with opaque keys and bounded concurrent cleanup."""

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE admission_buckets (
            scope text NOT NULL CHECK (scope IN ('login','refresh','start_user','start_ip')),
            bucket_key text NOT NULL CHECK (bucket_key ~ '^[0-9a-f]{64}$'),
            hits integer NOT NULL CHECK (hits > 0),
            expires_at timestamptz NOT NULL,
            PRIMARY KEY (scope,bucket_key)
        );
        CREATE INDEX admission_buckets_expiry ON admission_buckets(expires_at);
        REVOKE ALL ON admission_buckets FROM PUBLIC, scheduler_app;
        CREATE FUNCTION admission_admit(p_scope text, p_key text, p_limit integer,
                                        p_window_seconds integer)
        RETURNS TABLE(admitted boolean, retry_seconds integer)
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
        DECLARE current_hits integer; expiry timestamptz; admission_now timestamptz;
        BEGIN
            IF p_scope IS NULL OR p_key IS NULL OR p_limit IS NULL
               OR p_window_seconds IS NULL OR p_key !~ '^[0-9a-f]{64}$'
               OR NOT ((p_scope='login' AND p_limit=10 AND p_window_seconds=60)
                 OR (p_scope='refresh' AND p_limit=30 AND p_window_seconds=60)
                 OR (p_scope='start_user' AND p_limit=30 AND p_window_seconds=600)
                 OR (p_scope='start_ip' AND p_limit=120 AND p_window_seconds=600)) THEN
                RAISE EXCEPTION 'Invalid admission policy' USING ERRCODE='22023';
            END IF;
            -- Expired rows only; skip concurrent owners, and bound every call's work.
            DELETE FROM public.admission_buckets WHERE (scope,bucket_key) IN (
                SELECT b.scope,b.bucket_key FROM public.admission_buckets b
                WHERE b.expires_at <= clock_timestamp()
                ORDER BY b.expires_at LIMIT 100 FOR UPDATE SKIP LOCKED
            );
            -- One timestamp keeps count/expiry transitions coherent at the boundary.
            admission_now := clock_timestamp();
            INSERT INTO public.admission_buckets AS b(scope,bucket_key,hits,expires_at)
            VALUES(p_scope,p_key,1,admission_now + make_interval(secs=>p_window_seconds))
            ON CONFLICT(scope,bucket_key) DO UPDATE SET
                hits=CASE WHEN b.expires_at <= admission_now THEN 1
                          ELSE LEAST(b.hits+1,p_limit+1) END,
                expires_at=CASE WHEN b.expires_at <= admission_now
                    THEN admission_now+make_interval(secs=>p_window_seconds)
                    ELSE b.expires_at END
            RETURNING hits,expires_at INTO current_hits,expiry;
            RETURN QUERY SELECT current_hits <= p_limit,
                CASE WHEN current_hits <= p_limit THEN 0 ELSE
                    LEAST(3600,GREATEST(1,ceil(extract(epoch FROM expiry-clock_timestamp()))::integer)) END;
        END $$;
        REVOKE ALL ON FUNCTION admission_admit(text,text,integer,integer) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION admission_admit(text,text,integer,integer) TO scheduler_app;
    """)


def downgrade():
    op.execute("""
        DROP FUNCTION admission_admit(text,text,integer,integer);
        DROP TABLE admission_buckets;
    """)
