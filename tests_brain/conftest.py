"""Reuse the isolated Phase 1 database; never read workspace dotenv files."""

import pytest
from sqlalchemy.orm import Session

from scheduler.models import Agent, AgentVersion, Patient, Provider, Service
from tests_api.conftest import database as api_database
from tests_api.conftest import seed as api_seed


@pytest.fixture(scope="session")
def database():
    yield from api_database.__wrapped__()


@pytest.fixture
def seed(database):
    ids = api_seed.__wrapped__(database)
    with Session(database) as db, db.begin():
        for suffix in ("a", "b"):
            db.get(Patient, ids[f"patient_{suffix}"]).name_ur = "مصنوعی مریض"
            db.get(Provider, ids[f"provider_{suffix}"]).name_ur = "ڈاکٹر ٹیسٹ"
            db.get(Service, ids[f"service_{suffix}"]).name_ur = "مشورہ"
            agent = Agent(tenant_id=ids[f"tenant_{suffix}"], name="Synthetic Receptionist")
            db.add(agent)
            db.flush()
            version = AgentVersion(
                tenant_id=agent.tenant_id, agent_id=agent.id, config={"language_mode": "bilingual"}
            )
            db.add(version)
            db.flush()
            agent.active_version_id = version.id
            ids[f"agent_{suffix}"] = agent.id
            ids[f"version_{suffix}"] = version.id
    return ids
