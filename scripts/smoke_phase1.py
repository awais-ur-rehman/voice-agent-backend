"""Exercise the running local API using only the two provisioned synthetic clinics."""

import json
import math
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:8000"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def call(method, path, body=None, token=None, key=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if key:
        headers["Idempotency-Key"] = key
    request = Request(
        BASE + path,
        method=method,
        headers=headers,
        data=json.dumps(body).encode() if body is not None else None,
    )
    try:
        with build_opener(NoRedirect()).open(request, timeout=15) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except HTTPError as error:
        return error.code, json.loads(error.read())


def require(condition, message):
    if not condition:
        raise SystemExit("Smoke check failed: " + message)


def main():
    path = ROOT / ".local/phase1-demo-credentials.json"
    require(
        not path.is_symlink() and path.stat().st_mode & 0o777 == 0o600,
        "private demo credentials required",
    )
    clinics = json.loads(path.read_text())["clinics"]
    tokens = []
    for suffix, clinic in zip(("a", "b"), clinics, strict=True):
        require(
            clinic["owner_email"] == f"demo-owner-{suffix}@example.com",
            "use synthetic demo accounts only",
        )
        status, result = call(
            "POST",
            "/auth/login",
            {"email": clinic["owner_email"], "password": clinic["owner_password"]},
        )
        require(status == 200, "login")
        tokens.append(result["access_token"])
    a, b = clinics
    token = tokens[0]
    status, clinic = call("GET", "/clinic", token=token)
    require(status == 200 and clinic["id"] == a["tenant_id"], "tenant context")
    days = max(3, math.ceil(clinic["cancellation_min"] / 1440) + 1)
    require(
        days + 1 <= clinic["horizon_days"],
        "demo policy must allow two future dates beyond cancellation window",
    )
    first = (datetime.now(UTC) + timedelta(days=days)).date()

    def slots(day):
        query = f"/slots?service_id={a['service_id']}&provider_id={a['provider_id']}&date_from={day}&date_to={day}"
        status, result = call("GET", query, token=token)
        require(status == 200 and bool(result), "future available slots")
        return result

    start = slots(first)[0]["starts_at"]
    body = {
        "patient_id": a["patient_id"],
        "provider_id": a["provider_id"],
        "service_id": a["service_id"],
        "starts_at": start,
        "confirmed": True,
    }
    key = str(uuid4())
    status, booked = call("POST", "/appointments", body, token, key)
    require(status == 201, "booking")
    status, replay = call("POST", "/appointments", body, token, key)
    require(status == 201 and replay == booked, "exact idempotent replay")
    require(
        call("POST", "/appointments", body, token, str(uuid4()))[0] == 409,
        "duplicate slot conflict",
    )
    require(
        call("GET", "/appointments/" + booked["id"], token=tokens[1])[0] == 404,
        "cross-tenant appointment denial",
    )
    new_start = slots(first + timedelta(days=1))[0]["starts_at"]
    status, moved = call(
        "POST",
        "/appointments/" + booked["id"] + "/reschedule",
        {"starts_at": new_start, "confirmed": True},
        token,
        str(uuid4()),
    )
    require(status == 201 and moved["rescheduled_from_id"] == booked["id"], "atomic reschedule")
    require(
        call(
            "POST",
            "/appointments/" + moved["id"] + "/cancel",
            {"confirmed": True},
            token,
            str(uuid4()),
        )[0]
        == 200,
        "cancellation",
    )
    body["starts_at"] = new_start

    def compete(_):
        return call("POST", "/appointments", body, token, str(uuid4()))

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(compete, range(8)))
    require(sum(status == 201 for status, _ in results) == 1, "one concurrent winner")
    require(sum(status == 409 for status, _ in results) == 7, "seven concurrent conflicts")
    winner = next(result for status, result in results if status == 201)
    require(
        call(
            "POST",
            "/appointments/" + winner["id"] + "/cancel",
            {"confirmed": True},
            token,
            str(uuid4()),
        )[0]
        == 200,
        "race cleanup",
    )
    print(
        "PASS: two-clinic login, slots, booking, exact replay, conflict, cross-tenant denial, reschedule, cancel, and 8-request race (one winner)."
    )
    print("Synthetic smoke appointments are cancelled; audit/history remain for review.")


if __name__ == "__main__":
    main()
