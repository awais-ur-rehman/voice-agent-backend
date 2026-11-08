"""Index the bounded per-call measurement seek projection."""

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "CREATE INDEX analytics_stage_call_seek ON analytics_stage_metrics(tenant_id,session_id,observed_at,id)"
    )


def downgrade():
    op.execute("DROP INDEX analytics_stage_call_seek")
