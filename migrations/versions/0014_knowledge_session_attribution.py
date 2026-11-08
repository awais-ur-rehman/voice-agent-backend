"""Prospective server-owned knowledge observation attribution; no historical backfill."""

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    ALTER TABLE knowledge_query_events ADD COLUMN session_id uuid;
    ALTER TABLE knowledge_query_events ADD CONSTRAINT knowledge_observation_session_owner
      FOREIGN KEY(tenant_id,agent_id,session_id) REFERENCES agent_sessions(tenant_id,agent_id,id);
    CREATE INDEX knowledge_observation_session ON knowledge_query_events(tenant_id,session_id,created_at);
    """)


def downgrade():
    op.execute("ALTER TABLE knowledge_query_events DROP COLUMN session_id")
