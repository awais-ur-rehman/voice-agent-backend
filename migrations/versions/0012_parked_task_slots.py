"""Preserve opaque positional slots for every parked dialogue frame."""

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def _validator(*, nullable: bool) -> None:
    skip_null = "IF item='null'::jsonb THEN CONTINUE; END IF;" if nullable else ""
    op.execute(f"""
    CREATE OR REPLACE FUNCTION analytics_uuid_stack(value jsonb) RETURNS boolean
      LANGUAGE plpgsql IMMUTABLE SET search_path=pg_catalog AS $$
    DECLARE item jsonb;
    BEGIN
      IF jsonb_typeof(value)<>'array' THEN RETURN false; END IF;
      IF jsonb_array_length(value)>8 THEN RETURN false; END IF;
      FOR item IN SELECT * FROM jsonb_array_elements(value) LOOP
        {skip_null}
        IF jsonb_typeof(item)<>'string' OR (item #>> '{{}}') !~
          '^[0-9a-f]{{8}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{12}}$'
        THEN RETURN false; END IF;
      END LOOP;
      RETURN true;
    END $$;
    """)


def upgrade():
    _validator(nullable=True)


def downgrade():
    # Never collapse positional associations or install a validator that rejects
    # existing rows. Operators must finish/remove nullable stacks before rollback.
    op.execute("""
    DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM analytics_calls WHERE parked_task_ids @> '[null]'::jsonb)
      THEN RAISE EXCEPTION 'Cannot downgrade nullable parked task slots while in use';
      END IF;
    END $$;
    """)
    _validator(nullable=False)
