"""Exercise the console against a real local HTTP boundary with synthetic data."""

import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from brain.console import ConsoleClient, run_script


@pytest.fixture
def server():
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append((self.path, self.headers.get("Authorization"), body))
            if self.path == "/auth/login":
                payload = {"access_token": "synthetic-staff"}
            elif self.path == "/agent-sessions":
                payload = {
                    "session_id": "synthetic-session",
                    "token": "synthetic-runtime",
                    "revision": 0,
                    "state": {"lang": "ur"},
                }
            elif self.path.endswith("/renew"):
                payload = {"token": "rotated-runtime", "revision": 1, "state": {"lang": "ur"}}
            else:
                payload = {
                    "revision": len(seen) - 2,
                    "state": {"lang": "ur"},
                    "result": {"ok": True, "say": {"en": "Hello", "ur": "السلام علیکم"}},
                    "provider_requests": 1 if self.path.endswith("/turn") else 0,
                }
            encoded = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}", seen
    httpd.shutdown()
    httpd.server_close()
    thread.join()


def test_console_uses_scoped_tokens_and_current_revision_without_echoing_secrets(server):
    url, seen = server
    output = io.StringIO()
    client = ConsoleClient(url)
    client.login("owner@example.com", "synthetic-password")
    client.create("agent-1")
    run_script(client, [{"text": "hello"}, {"text": "again"}], live=True, output=output)
    assert seen[1] == ("/agent-sessions", "Bearer synthetic-staff", {"agent_id": "agent-1"})
    assert seen[2][1] == "Bearer synthetic-runtime"
    assert seen[2][2]["expected_revision"] == 0
    assert seen[3][2]["expected_revision"] == 1
    assert seen[2][2]["live"] is True
    assert seen[2][2]["command_id"] != seen[3][2]["command_id"]
    assert output.getvalue().count("السلام علیکم") == 2
    assert "synthetic-password" not in output.getvalue()
    assert "synthetic-runtime" not in output.getvalue()
    assert "hello" not in output.getvalue()
    assert client.http_commands == 2
    assert client.provider_requests == 2


def test_scripted_commands_are_labelled_without_claiming_inference(server):
    url, seen = server
    client = ConsoleClient(url)
    client.login("owner@example.com", "synthetic-password")
    client.create("agent-1")
    action = {"name": "clinic_facts", "arguments": {}}
    run_script(client, [{"action": action, "text": "hours"}], output=io.StringIO())
    assert seen[-1][0].endswith("/commands")
    assert seen[-1][2]["action"] == action
    assert client.provider_requests == 0
    client.renew()
    assert client.token == "rotated-runtime"


def test_console_refuses_remote_plain_http_to_protect_capabilities():
    with pytest.raises(ValueError):
        ConsoleClient("http://clinic.example.com")


def test_published_agent_configuration_is_bounded_and_rejects_unknown_security_fields():
    from pydantic import ValidationError

    from brain.bootstrap import AgentConfig

    config = AgentConfig(
        language_mode="ur", instructions="Respectful concise Urdu.", read_digits_in_english=True
    )
    assert config.model_dump()["read_digits_in_english"] is True
    with pytest.raises(ValidationError):
        AgentConfig(language_mode="xx")
    with pytest.raises(ValidationError):
        AgentConfig(instructions="x" * 2001)
    with pytest.raises(ValidationError):
        AgentConfig(verified=True)


def test_demo_credentials_reader_requires_private_regular_file(tmp_path):
    from brain.bootstrap import read_demo_credentials

    path = tmp_path / "demo.json"
    path.write_text('{"clinics": [{"tenant_id": "synthetic"}]}')
    path.chmod(0o644)
    with pytest.raises(ValueError):
        read_demo_credentials(path)
    path.chmod(0o600)
    assert read_demo_credentials(path)["clinics"][0]["tenant_id"] == "synthetic"
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(ValueError):
        read_demo_credentials(link)


def test_demo_identifier_update_preserves_credentials_and_is_idempotent(tmp_path):
    from brain.bootstrap import update_demo_identifiers

    path = tmp_path / "demo.json"
    credentials = {
        "clinics": [
            {
                "tenant_id": "tenant-a",
                "owner_password": "saved-password",
                "owner_email": "synthetic@example.com",
            }
        ]
    }
    path.write_text(json.dumps(credentials))
    path.chmod(0o600)
    update_demo_identifiers(path, [{"tenant_id": "tenant-a", "agent_id": "agent-a"}])
    result = json.loads(path.read_text())
    assert result["clinics"][0] == {
        "tenant_id": "tenant-a",
        "owner_password": "saved-password",
        "owner_email": "synthetic@example.com",
        "agent_id": "agent-a",
    }
    before = path.stat()
    update_demo_identifiers(path, [{"tenant_id": "tenant-a", "agent_id": "agent-a"}])
    assert path.stat().st_mtime_ns == before.st_mtime_ns
    assert path.stat().st_mode & 0o777 == 0o600


def test_publish_creates_new_versions_without_mutating_existing_configuration():
    import os
    from uuid import uuid4

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from brain.bootstrap import AgentConfig, publish_agent
    from scheduler.models import Agent, AgentVersion, Tenant

    # Dedicated test database only, unique rows, rolled back; no global fixture cleanup.
    url = os.environ.get(
        "BRAIN_CLI_TEST_DATABASE_URL",
        "postgresql+psycopg://postgres:phase1_test_only@127.0.0.1:55433/voice_agent_test",
    )
    from sqlalchemy.engine import make_url

    if make_url(url).database != "voice_agent_test":
        raise RuntimeError("Publisher test requires the dedicated test database")
    engine = create_engine(url, hide_parameters=True)
    try:
        with Session(engine) as db:
            transaction = db.begin()
            try:
                tenant = Tenant(id=uuid4(), name="Synthetic CLI test")
                db.add(tenant)
                db.flush()
                agent = Agent(id=uuid4(), tenant_id=tenant.id, name="Synthetic CLI agent")
                db.add(agent)
                db.flush()
                first = publish_agent(db, tenant.id, agent.id, AgentConfig(language_mode="en"))
                second = publish_agent(db, tenant.id, agent.id, AgentConfig(language_mode="ur"))
                assert first != second
                assert agent.active_version_id == second
                assert db.get(AgentVersion, first).config["language_mode"] == "en"
                assert db.get(AgentVersion, second).config["language_mode"] == "ur"
                with pytest.raises(ValueError):
                    publish_agent(db, uuid4(), agent.id, AgentConfig())
            finally:
                transaction.rollback()
    finally:
        engine.dispose()


def test_published_prayer_break_and_bilingual_public_profile_are_validated():
    from pydantic import ValidationError

    from brain.bootstrap import AgentConfig

    config = AgentConfig.model_validate(
        {
            "prayer_break": ["13:00", "14:30"],
            "clinic_profile": {"address_en": "Synthetic Road", "address_ur": "نمونہ سڑک"},
        }
    )
    assert config.prayer_break == ["13:00", "14:30"]
    with pytest.raises(ValidationError):
        AgentConfig.model_validate({"prayer_break": ["14:30", "13:00"]})
    with pytest.raises(ValidationError):
        AgentConfig.model_validate({"clinic_profile": {"tenant_id": "injected"}})
