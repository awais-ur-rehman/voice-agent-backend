"""Add internal provider route provenance without changing existing observations."""

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        ALTER TABLE analytics_usage_invocations
          ADD COLUMN route text NOT NULL DEFAULT 'primary'
            CHECK (route IN ('primary','secondary')),
          ADD COLUMN fallback_from uuid,
          DROP CONSTRAINT analytics_usage_invocations_provider_check,
          ADD CONSTRAINT analytics_usage_invocations_provider_check
            CHECK (provider IN ('nim','openai_compatible'));
    """)


def downgrade():
    # Cannot truthfully relabel existing secondary usage as NIM; fail on such rows.
    op.execute("""
        ALTER TABLE analytics_usage_invocations
          DROP COLUMN fallback_from,
          DROP COLUMN route,
          DROP CONSTRAINT analytics_usage_invocations_provider_check,
          ADD CONSTRAINT analytics_usage_invocations_provider_check CHECK (provider IN ('nim'));
    """)
