"""Disposable PostgreSQL only; never load workspace dotenv files or real credentials."""

import os
from datetime import UTC, datetime, time
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from scheduler.config import Settings
from scheduler.main import create_app
from scheduler.models import Patient, Provider, ProviderService, Schedule, Service, Tenant, User
from scheduler.security import passwords

ROOT = Path(__file__).resolve().parents[1]
ADMIN_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://postgres:phase1_test_only@127.0.0.1:55433/voice_agent_test",
)
APP_URL = os.environ.get(
    "TEST_APP_DATABASE_URL",
    "postgresql+psycopg://scheduler_app:phase1_app_test_only@127.0.0.1:55433/voice_agent_test",
)
SECRET = "isolated-test-secret-not-a-production-credential-2026"
PASSWORD = "Test-password-2026!"
NOW = datetime(2026, 11, 1, tzinfo=UTC)


@pytest.fixture(scope="session")
def database():
    if (
        make_url(ADMIN_URL).database != "voice_agent_test"
        or make_url(APP_URL).database != "voice_agent_test"
    ):
        raise RuntimeError("Tests refuse cleanup outside the dedicated voice_agent_test database")
    from scripts.init_db import init_database

    init_database(ADMIN_URL, os.environ.get("TEST_APP_PASSWORD", "phase1_app_test_only"))
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", ADMIN_URL.replace("%", "%%"))
    command.upgrade(cfg, "head")
    engine = create_engine(ADMIN_URL, hide_parameters=True)
    yield engine
    engine.dispose()


@pytest.fixture
def seed(database):
    with database.begin() as conn:
        conn.execute(text("TRUNCATE tenants CASCADE"))
    ids = {}
    with Session(database, expire_on_commit=False) as db, db.begin():
        for suffix in ("a", "b"):
            tenant = Tenant(id=uuid4(), name=f"Test Clinic {suffix}", timezone="Asia/Karachi")
            db.add(tenant)
            db.flush()
            for role in ("owner", "staff"):
                user = User(
                    id=uuid4(),
                    tenant_id=tenant.id,
                    email=f"{role}-{suffix}@example.com",
                    password_hash=passwords.hash(PASSWORD),
                    role=role,
                    active=True,
                )
                db.add(user)
                ids[f"{role}_{suffix}"] = user.id
            provider = Provider(id=uuid4(), tenant_id=tenant.id, name=f"Dr Test {suffix}")
            service = Service(id=uuid4(), tenant_id=tenant.id, name="Consultation", duration_min=30)
            patient = Patient(
                id=uuid4(),
                tenant_id=tenant.id,
                name="Synthetic Patient",
                name_key="synthetic patient",
                phone="+923001234567",
            )
            db.add_all([provider, service, patient])
            db.flush()
            db.add(
                ProviderService(tenant_id=tenant.id, provider_id=provider.id, service_id=service.id)
            )
            for weekday in range(1, 8):
                db.add(
                    Schedule(
                        id=uuid4(),
                        tenant_id=tenant.id,
                        provider_id=provider.id,
                        weekday=weekday,
                        start_time=time(9),
                        end_time=time(17),
                        slot_min=30,
                    )
                )
            ids |= {
                f"tenant_{suffix}": tenant.id,
                f"provider_{suffix}": provider.id,
                f"service_{suffix}": service.id,
                f"patient_{suffix}": patient.id,
            }
    return ids


@pytest.fixture
def client(seed):
    settings = Settings(database_url=APP_URL, jwt_secret=SECRET, _env_file=None)
    app = create_app(settings, clock=lambda: NOW)
    with TestClient(app) as client:
        yield client
    app.state.engine.dispose()


@pytest.fixture
def owner(client):
    result = client.post("/auth/login", json={"email": "owner-a@example.com", "password": PASSWORD})
    assert result.status_code == 200, result.text
    client.headers["Authorization"] = "Bearer " + result.json()["access_token"]
    return client
