"""Snapshot the code-owned recording notice without an arbitrary text field."""

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE analytics_calls ADD COLUMN consent_notice_version text CHECK (consent_notice_version IS NULL OR consent_notice_version='recording-v1')"
    )


def downgrade():
    op.execute("ALTER TABLE analytics_calls DROP COLUMN consent_notice_version")
