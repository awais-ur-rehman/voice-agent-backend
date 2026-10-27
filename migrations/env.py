import os
from pathlib import Path

from alembic import context
from dotenv import dotenv_values
from sqlalchemy import create_engine

config = context.config
url = config.get_main_option("sqlalchemy.url") or os.environ.get("SCHEDULER_MIGRATION_DATABASE_URL")
if not url:
    url = dotenv_values(Path(__file__).resolve().parents[1] / ".env.api").get(
        "SCHEDULER_MIGRATION_DATABASE_URL"
    )
if not url:
    raise RuntimeError("Configure a separate migration database URL")
engine = create_engine(url, hide_parameters=True)
with engine.connect() as connection:
    context.configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()
engine.dispose()
