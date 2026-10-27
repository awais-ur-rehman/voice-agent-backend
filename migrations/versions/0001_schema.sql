CREATE EXTENSION IF NOT EXISTS btree_gist;
CREATE TABLE tenants (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), name text NOT NULL,
 timezone text NOT NULL DEFAULT 'Asia/Karachi', currency text NOT NULL DEFAULT 'PKR',
 min_notice_min integer NOT NULL DEFAULT 120 CHECK (min_notice_min BETWEEN 0 AND 43200),
 horizon_days integer NOT NULL DEFAULT 90 CHECK (horizon_days BETWEEN 1 AND 365),
 cancellation_min integer NOT NULL DEFAULT 1440 CHECK (cancellation_min BETWEEN 0 AND 43200),
 daily_limit integer NOT NULL DEFAULT 3 CHECK (daily_limit BETWEEN 1 AND 100)
);
CREATE TABLE users (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants ON DELETE CASCADE,
 email text NOT NULL, password_hash text NOT NULL, role text NOT NULL CHECK(role IN ('owner','admin','staff')),
 active boolean NOT NULL DEFAULT true, UNIQUE(tenant_id,id)
);
CREATE UNIQUE INDEX users_email_unique ON users(lower(email));
CREATE TABLE refresh_tokens (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants ON DELETE CASCADE,
 user_id uuid NOT NULL, family_id uuid NOT NULL, token_hash text NOT NULL UNIQUE,
 expires_at timestamptz NOT NULL, revoked_at timestamptz,
 FOREIGN KEY(tenant_id,user_id) REFERENCES users(tenant_id,id)
);
CREATE INDEX refresh_family ON refresh_tokens(tenant_id,family_id);
CREATE TABLE providers (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants ON DELETE CASCADE,
 name text NOT NULL, specialty text NOT NULL DEFAULT '', active boolean NOT NULL DEFAULT true, UNIQUE(tenant_id,id)
);
CREATE TABLE services (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants ON DELETE CASCADE,
 name text NOT NULL, duration_min integer NOT NULL CHECK(duration_min BETWEEN 1 AND 480),
 buffer_before_min integer NOT NULL DEFAULT 0 CHECK(buffer_before_min BETWEEN 0 AND 120),
 buffer_after_min integer NOT NULL DEFAULT 0 CHECK(buffer_after_min BETWEEN 0 AND 120),
 price numeric(12,2) NOT NULL DEFAULT 0 CHECK(price>=0), active boolean NOT NULL DEFAULT true, UNIQUE(tenant_id,id)
);
CREATE TABLE provider_services (
 tenant_id uuid NOT NULL REFERENCES tenants ON DELETE CASCADE,
 provider_id uuid NOT NULL, service_id uuid NOT NULL, PRIMARY KEY(provider_id,service_id),
 FOREIGN KEY(tenant_id,provider_id) REFERENCES providers(tenant_id,id),
 FOREIGN KEY(tenant_id,service_id) REFERENCES services(tenant_id,id)
);
CREATE TABLE availability_rules (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants ON DELETE CASCADE,
 provider_id uuid NOT NULL, weekday integer NOT NULL CHECK(weekday BETWEEN 1 AND 7),
 start_time time NOT NULL, end_time time NOT NULL, slot_min integer NOT NULL CHECK(slot_min BETWEEN 5 AND 240),
 CHECK(start_time<end_time), FOREIGN KEY(tenant_id,provider_id) REFERENCES providers(tenant_id,id)
);
CREATE INDEX availability_provider ON availability_rules(tenant_id,provider_id,weekday);
CREATE TABLE availability_exceptions (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants ON DELETE CASCADE,
 provider_id uuid NOT NULL, start_date date NOT NULL, end_date date NOT NULL,
 kind text NOT NULL CHECK(kind IN ('closed','custom')), start_time time, end_time time,
 CHECK(start_date<=end_date), CHECK((kind='closed' AND start_time IS NULL AND end_time IS NULL)
 OR (kind='custom' AND start_time IS NOT NULL AND end_time IS NOT NULL AND start_time<end_time)),
 FOREIGN KEY(tenant_id,provider_id) REFERENCES providers(tenant_id,id)
);
CREATE INDEX exceptions_provider ON availability_exceptions(tenant_id,provider_id,start_date,end_date);
CREATE TABLE patients (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants ON DELETE CASCADE,
 name text NOT NULL, name_key text NOT NULL, phone text NOT NULL, dob date,
 UNIQUE(tenant_id,id), UNIQUE(tenant_id,phone,name_key)
);
CREATE TABLE appointments (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants ON DELETE CASCADE,
 provider_id uuid NOT NULL, patient_id uuid NOT NULL, service_id uuid NOT NULL,
 starts_at timestamptz NOT NULL, ends_at timestamptz NOT NULL,
 duration_min integer NOT NULL CHECK(duration_min BETWEEN 1 AND 480),
 buffer_before_min integer NOT NULL CHECK(buffer_before_min BETWEEN 0 AND 120),
 buffer_after_min integer NOT NULL CHECK(buffer_after_min BETWEEN 0 AND 120),
 occupied_starts_at timestamptz NOT NULL, occupied_ends_at timestamptz NOT NULL,
 status text NOT NULL DEFAULT 'booked' CHECK(status IN ('booked','cancelled','completed','no_show')),
 rescheduled_from_id uuid, created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(tenant_id,id),
 CHECK(starts_at<ends_at), CHECK(occupied_starts_at<=starts_at AND occupied_ends_at>=ends_at),
 FOREIGN KEY(tenant_id,provider_id) REFERENCES providers(tenant_id,id),
 FOREIGN KEY(tenant_id,patient_id) REFERENCES patients(tenant_id,id),
 FOREIGN KEY(tenant_id,service_id) REFERENCES services(tenant_id,id),
 FOREIGN KEY(tenant_id,rescheduled_from_id) REFERENCES appointments(tenant_id,id),
 EXCLUDE USING gist(tenant_id WITH =, provider_id WITH =,
 tstzrange(occupied_starts_at,occupied_ends_at,'[)') WITH &&) WHERE(status='booked')
);
CREATE INDEX appointments_patient ON appointments(tenant_id,patient_id,starts_at);
CREATE FUNCTION appointment_boundaries() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
BEGIN
 NEW.ends_at := NEW.starts_at + NEW.duration_min * interval '1 minute';
 NEW.occupied_starts_at := NEW.starts_at - NEW.buffer_before_min * interval '1 minute';
 NEW.occupied_ends_at := NEW.ends_at + NEW.buffer_after_min * interval '1 minute';
 RETURN NEW;
END $$;
CREATE TRIGGER appointment_boundaries BEFORE INSERT OR UPDATE ON appointments
FOR EACH ROW EXECUTE FUNCTION appointment_boundaries();
CREATE TABLE idempotency_records (
 tenant_id uuid NOT NULL REFERENCES tenants ON DELETE CASCADE, key text NOT NULL,
 operation text NOT NULL, request_hash text NOT NULL, response jsonb, created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,key)
);
CREATE TABLE audit_log (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants ON DELETE CASCADE,
 actor_id uuid NOT NULL, action text NOT NULL, entity_id uuid NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 FOREIGN KEY(tenant_id,actor_id) REFERENCES users(tenant_id,id)
);
