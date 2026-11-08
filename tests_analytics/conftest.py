"""Reuse the guarded disposable database, serially with the project suite."""

from uuid import uuid4

import pytest
from sqlalchemy import text

from tests_api.conftest import database, seed  # noqa: F401


@pytest.fixture
def analytics_seed(database, seed):  # noqa: F811
    ids = dict(seed)
    with database.begin() as db:
        for suffix in ("a", "b"):
            values = dict(
                tenant=ids[f"tenant_{suffix}"], agent=uuid4(), version=uuid4(), session=uuid4()
            )
            db.execute(
                text("INSERT INTO agents(id,tenant_id,name) VALUES(:agent,:tenant,'Synthetic')"),
                values,
            )
            db.execute(
                text(
                    "INSERT INTO agent_versions(id,tenant_id,agent_id,config) VALUES(:version,:tenant,:agent,'{}')"
                ),
                values,
            )
            db.execute(
                text(
                    "INSERT INTO agent_sessions(id,tenant_id,agent_id,version_id,token_jti_hash,expires_at) VALUES(:session,:tenant,:agent,:version,'synthetic',now()+interval '1 hour')"
                ),
                values,
            )
            ids.update({f"{key}_{suffix}": value for key, value in values.items()})
    return ids


def insert_call(db, ids, suffix="a", **overrides):
    values = (
        dict(
            tenant_id=ids[f"tenant_{suffix}"],
            agent_id=ids[f"agent_{suffix}"],
            session_id=ids[f"session_{suffix}"],
            trace_id=uuid4(),
            purpose="production",
            channel="text",
            timezone="Asia/Karachi",
            local_date="2026-11-08",
            utc_offset_minutes=300,
            started_at="2026-11-08T08:00:00Z",
            expires_at="2026-11-08T09:00:00Z",
        )
        | overrides
    )
    db.execute(
        text(
            f"INSERT INTO analytics_calls({','.join(values)}) VALUES({','.join(':' + key for key in values)})"
        ),
        values,
    )
    return values
