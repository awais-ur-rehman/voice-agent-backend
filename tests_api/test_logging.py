import json
import logging


def test_structured_request_logs_never_include_query_body_or_credentials(owner):
    records = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    logger = logging.getLogger("scheduler.requests")
    handler = Capture()
    logger.addHandler(handler)
    try:
        owner.get("/patients", params={"search": "DO_NOT_LOG_QUERY"})
        owner.post("/patients", json={"name": "DO_NOT_LOG_BODY", "phone": "bad"})
    finally:
        logger.removeHandler(handler)
    assert len(records) == 2
    events = [json.loads(value) for value in records]
    assert {event["route"] for event in events} == {"/patients"}
    assert "DO_NOT_LOG" not in "".join(records)
    assert "Authorization" not in "".join(records)
    assert all("request_id" in event and "duration_ms" in event for event in events)
