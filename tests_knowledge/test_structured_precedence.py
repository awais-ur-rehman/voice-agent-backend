"""Structured facts outrank contradictory documents through both runtime entry paths."""

from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from brain.knowledge import local_input_action
from knowledge.models import QueryEvent
from scheduler.models import Agent, AgentSession, AgentVersion
from tests_knowledge.test_retrieval import Embeddings
from tests_knowledge.test_runtime import dashboard as runtime_dashboard
from tests_knowledge.test_runtime import seed as runtime_seed
from tests_knowledge.test_runtime import setup_runtime


@pytest.fixture
def seed(database):
    return runtime_seed.__wrapped__(database)


@pytest.fixture
def dashboard(seed):
    yield from runtime_dashboard.__wrapped__(seed)


@pytest.mark.parametrize(
    "question,topic",
    [
        ("When do you close?", "hours"),
        ("What time does the clinic close?", "hours"),
        ("What are your closing times?", "hours"),
        ("Are you still open?", "hours"),
        ("کلینک کب بند ہوتا ہے؟", "hours"),
        ("آپ کتنے بجے بند کرتے ہیں؟", "hours"),
        ("Where is the clinic?", "location"),
        ("Is the clinic located near the station?", "location"),
        ("Where are you located?", "location"),
        ("کلینک کہاں واقع ہے؟", "location"),
        ("How should I prepare?", "preparation"),
        ("Do I need to bring documents?", "preparation"),
        ("کیا دستاویزات لاؤں؟", "preparation"),
        ("Where is bicycle parking?", None),
        ("Is bicycle parking open?", None),
        ("How do I open the patient portal?", None),
        ("Where can I bring my bicycle?", None),
        ("Do I need to bring my bicycle?", None),
    ],
)
@pytest.mark.parametrize("locale", ["en", "ur"])
def test_owned_domains_do_not_swallow_unrelated_faq(question, topic, locale):
    config = {
        "clinic_profile": {f"address_{locale}": "Configured", f"preparation_{locale}": "Configured"}
    }
    action, clarify = local_input_action({}, question, config)
    assert not clarify
    assert (
        action == {"name": "get_clinic_info", "arguments": {"topic": topic}}
        if topic
        else action == {"name": "search_knowledge", "arguments": {}}
    )


@pytest.mark.parametrize("entry", ["turn", "commands"])
@pytest.mark.parametrize(
    "question,lang,profile,expected",
    [
        ("When do you close?", "en", {}, "five in the evening"),
        ("Are you still open?", "en", {}, "five in the evening"),
        ("کلینک کب بند ہوتا ہے؟", "ur", {}, "شام پانچ"),
        ("Where is the clinic?", "en", {"address_en": "10 Correct Road"}, "10 Correct Road"),
        (
            "Is the clinic located near the station?",
            "en",
            {"address_en": "10 Correct Road", "address_ur": "درست پتہ"},
            "10 Correct Road",
        ),
        ("کلینک کہاں واقع ہے؟", "ur", {"address_ur": "درست پتہ"}, "درست پتہ"),
        (
            "Where is the clinic?",
            "en",
            {"address_ur": "درست پتہ"},
            "I do not have a verified answer.",
        ),
        (
            "کلینک کہاں واقع ہے؟",
            "ur",
            {"address_en": "10 Correct Road"},
            "میرے پاس تصدیق شدہ جواب نہیں۔",
        ),
        (
            "How should I prepare?",
            "en",
            {"preparation_en": "Bring the reviewed checklist."},
            "Bring the reviewed checklist.",
        ),
        (
            "Do I need to bring documents?",
            "en",
            {
                "preparation_en": "Bring the reviewed checklist.",
                "preparation_ur": "تصدیق شدہ فہرست لائیں۔",
            },
            "Bring the reviewed checklist.",
        ),
        (
            "کیا دستاویزات لاؤں؟",
            "ur",
            {"preparation_ur": "تصدیق شدہ فہرست لائیں۔"},
            "تصدیق شدہ فہرست لائیں۔",
        ),
        (
            "What documents should I bring?",
            "en",
            {"preparation_ur": "تصدیق شدہ فہرست لائیں۔"},
            "I do not have a verified answer.",
        ),
        (
            "کیا دستاویزات لاؤں؟",
            "ur",
            {"preparation_en": "Bring the reviewed checklist."},
            "میرے پاس تصدیق شدہ جواب نہیں۔",
        ),
    ],
)
def test_owned_fact_conflict_skips_all_providers(
    dashboard, database, seed, entry, question, lang, profile, expected
):
    _, provider, *_ = setup_runtime(
        dashboard,
        database,
        seed,
        "The clinic closes at midnight at 99 Wrong Road. No documents are needed.",
    )
    with Session(database) as db, db.begin():
        version = AgentVersion(
            tenant_id=seed["tenant_a"],
            agent_id=seed["agent_a"],
            config={"clinic_profile": profile, "language_mode": lang},
        )
        db.add(version)
        db.flush()
        db.get(Agent, seed["agent_a"]).active_version_id = version.id

    class CountingEmbedding(Embeddings):
        calls = 0

        def embed(self, *args, **kwargs):
            self.calls += 1
            return super().embed(*args, **kwargs)

    embeddings = CountingEmbedding()
    dashboard.app.state.knowledge_embeddings = embeddings
    created = dashboard.post("/agent-sessions", json={"agent_id": str(seed["agent_a"])}).json()
    with Session(database) as db, db.begin():
        session = db.get(AgentSession, UUID(created["session_id"]))
        session.checkpoint = session.checkpoint | {"lang": lang}
    body = {"command_id": "owned-fact", "expected_revision": 0, "text": question}
    if entry == "commands":
        body["action"] = {"name": "search_knowledge", "arguments": {}}
    response = dashboard.post(
        f"/runtime/sessions/{created['session_id']}/{entry}",
        headers={"Authorization": "Bearer " + created["token"]},
        json=body,
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["executed_action"] == "get_clinic_info"
    assert expected in result["result"]["say"][lang]
    assert "Wrong Road" not in str(result)
    assert result["provider_requests"] == 0
    assert embeddings.calls == 0 and provider.calls == []
    with Session(database) as db:
        assert list(db.scalars(select(QueryEvent))) == []


def test_parking_still_reaches_knowledge_with_configured_address(dashboard, database, seed):
    from scheduler.models import Tenant

    with Session(database) as db, db.begin():
        clinic = db.get(Tenant, seed["tenant_a"])
        clinic.address_en, clinic.address_ur = "10 Correct Road", "درست پتہ"
    test, provider, *_ = setup_runtime(
        dashboard, database, seed, "Bicycle parking is beside the gate."
    )
    from tests_brain.test_draft_tests import send

    response = send(dashboard, test["id"], 0, "Where is bicycle parking?")
    assert response.status_code == 200, response.text
    assert response.json()["knowledge"]["status"] == "ready"
    assert response.json()["messages"][0]["text"] == "Bicycle parking is beside the gate."
    assert provider.calls == [("Where is bicycle parking?", "en")]
