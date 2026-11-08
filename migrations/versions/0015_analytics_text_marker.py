"""Prospective nontext capture marker survives text expiry; no historical inference."""

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE analytics_calls ADD COLUMN transcript_captured_at timestamptz")


def downgrade():
    op.execute("ALTER TABLE analytics_calls DROP COLUMN transcript_captured_at")
