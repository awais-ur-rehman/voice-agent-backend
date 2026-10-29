"""Clinic profiles, canonical patient identities and narrow registration."""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE EXTENSION IF NOT EXISTS pg_trgm;
    ALTER TABLE tenants ADD COLUMN revision integer NOT NULL DEFAULT 1 CHECK(revision>0),
      ADD COLUMN address_en text NOT NULL DEFAULT '', ADD COLUMN address_ur text NOT NULL DEFAULT '',
      ADD COLUMN preparation_en text NOT NULL DEFAULT '', ADD COLUMN preparation_ur text NOT NULL DEFAULT '';
    ALTER TABLE users ADD COLUMN display_name text;
    ALTER TABLE patients ADD COLUMN revision integer NOT NULL DEFAULT 1 CHECK(revision>0),
      ADD COLUMN merged_into_id uuid,
      ADD CONSTRAINT patient_merge_target FOREIGN KEY(tenant_id,merged_into_id) REFERENCES patients(tenant_id,id),
      ADD CONSTRAINT patient_merge_not_self CHECK(merged_into_id IS DISTINCT FROM id);
    ALTER TABLE patients DROP CONSTRAINT patients_tenant_id_phone_name_key_key;
    CREATE UNIQUE INDEX patients_active_identity ON patients(tenant_id,phone,name_key) WHERE merged_into_id IS NULL;
    CREATE INDEX patients_name_trgm ON patients USING gin(name gin_trgm_ops) WHERE merged_into_id IS NULL;
    CREATE INDEX patients_name_ur_trgm ON patients USING gin(name_ur gin_trgm_ops) WHERE merged_into_id IS NULL;
    CREATE INDEX patients_name_simple ON patients USING gin(to_tsvector('simple'::regconfig,
      coalesce(name,'') || ' ' || coalesce(name_ur,''))) WHERE merged_into_id IS NULL;
    CREATE FUNCTION dashboard_revision() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
    BEGIN NEW.revision := OLD.revision+1; RETURN NEW; END $$;
    CREATE TRIGGER clinic_revision BEFORE UPDATE ON tenants FOR EACH ROW EXECUTE FUNCTION dashboard_revision();
    CREATE TRIGGER patient_revision BEFORE UPDATE ON patients FOR EACH ROW EXECUTE FUNCTION dashboard_revision();
    CREATE TABLE registration_attempts (bucket text PRIMARY KEY, window_start timestamptz NOT NULL, attempts integer NOT NULL);
    REVOKE ALL ON registration_attempts FROM PUBLIC,scheduler_app;
    CREATE FUNCTION registration_admit(bucket_key text) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
    DECLARE n integer;
    BEGIN
      INSERT INTO public.registration_attempts VALUES(bucket_key,clock_timestamp(),1)
      ON CONFLICT(bucket) DO UPDATE SET
        attempts=CASE WHEN registration_attempts.window_start < clock_timestamp()-interval '1 hour' THEN 1 ELSE registration_attempts.attempts+1 END,
        window_start=CASE WHEN registration_attempts.window_start < clock_timestamp()-interval '1 hour' THEN clock_timestamp() ELSE registration_attempts.window_start END
      RETURNING attempts INTO n;
      RETURN n<=5;
    END $$;
    CREATE FUNCTION provision_clinic(clinic_name text, clinic_zone text, owner_email text, owner_hash text, owner_name text)
      RETURNS TABLE(user_id uuid,clinic_id uuid) LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
    DECLARE tid uuid := gen_random_uuid(); uid uuid := gen_random_uuid();
    BEGIN
      INSERT INTO public.tenants(id,name,timezone) VALUES(tid,clinic_name,clinic_zone);
      INSERT INTO public.users(id,tenant_id,email,password_hash,role,display_name) VALUES(uid,tid,lower(owner_email),owner_hash,'owner',owner_name);
      RETURN QUERY SELECT uid,tid;
    END $$;
    REVOKE ALL ON FUNCTION provision_clinic(text,text,text,text,text), registration_admit(text) FROM PUBLIC;
    GRANT EXECUTE ON FUNCTION provision_clinic(text,text,text,text,text), registration_admit(text) TO scheduler_app;
    """)


def downgrade():
    op.execute("""
    DROP FUNCTION provision_clinic(text,text,text,text,text); DROP FUNCTION registration_admit(text);
    DROP TABLE registration_attempts;
    DROP TRIGGER clinic_revision ON tenants; DROP TRIGGER patient_revision ON patients; DROP FUNCTION dashboard_revision();
    DROP INDEX IF EXISTS patients_name_simple; DROP INDEX IF EXISTS patients_name_ur_trgm; DROP INDEX IF EXISTS patients_name_trgm;
    DROP INDEX patients_active_identity;
    ALTER TABLE patients DROP CONSTRAINT patient_merge_target, DROP CONSTRAINT patient_merge_not_self,
      DROP COLUMN merged_into_id, DROP COLUMN revision;
    ALTER TABLE patients ADD UNIQUE(tenant_id,phone,name_key);
    ALTER TABLE users DROP COLUMN display_name;
    ALTER TABLE tenants DROP COLUMN revision, DROP COLUMN address_en, DROP COLUMN address_ur,
      DROP COLUMN preparation_en, DROP COLUMN preparation_ur;
    """)
