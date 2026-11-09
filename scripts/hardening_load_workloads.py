"""Fixed synthetic dataset and HTTP measurements for the opt-in load CLI."""

import asyncio
import sys
from collections import Counter
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from time import perf_counter
from unittest.mock import patch
from uuid import UUID, uuid5

import httpx2 as httpx
from sqlalchemy import create_engine, delete, select, text
from sqlalchemy.orm import Session

from brain import draft_tests, voice_routes
from brain.contracts import parse_action
from brain.state import recovery_checkpoint
from scheduler.admission import bucket_key
from scheduler.config import Settings
from scheduler.main import create_app
from scheduler.models import (
    Agent,
    AgentSession,
    AgentVersion,
    Appointment,
    CommandReceipt,
    Idempotency,
    Patient,
    Provider,
    ProviderService,
    Schedule,
    Service,
    Tenant,
    User,
)
from scheduler.security import access_token

from .hardening_load import (
    FIXTURE_ENV,
    SCHEMA,
    TARGET_MS,
    Profile,
    Refusal,
    quantile,
    require_database,
    source_context,
)

NOW = datetime(2026, 11, 9, tzinfo=UTC)
PEER = "127.0.0.7"
NAMESPACE = UUID("39139f97-75ce-4742-9707-6be0c1b46363")
ERROR_CODES = {
    "admission_unavailable",
    "conflict",
    "database_unavailable",
    "idempotency_conflict",
    "internal_error",
    "not_found",
    "outside_policy",
    "rate_limited",
    "slot_unavailable",
    "transaction_conflict",
    "unauthorized",
    "validation_error",
}


def identity(label: str) -> UUID:
    return uuid5(NAMESPACE, label)


def slot(day: int, offset: int = 0) -> datetime:
    return NOW + timedelta(days=day, hours=9, minutes=30 * offset)


@dataclass(frozen=True)
class Clinic:
    label: str

    @property
    def tenant(self) -> UUID:
        return identity(self.label)

    @property
    def name(self) -> str:
        return f"Phase 7 synthetic {self.label}"

    @property
    def user(self) -> UUID:
        return identity(f"{self.label}:owner")

    @property
    def service(self) -> UUID:
        return identity(f"{self.label}:service")

    def provider(self, index: int) -> UUID:
        return identity(f"{self.label}:provider:{index}")

    def patient(self, index: int) -> UUID:
        return identity(f"{self.label}:patient:{index}")


INFO = Clinic("search-information")
BOOKING = Clinic("booking")
AGENT = identity("information-agent")


class OfflineAdapters:
    def __init__(self):
        self.plan_calls = 0
        self.external_attempts = 0

    def plan(self, state, utterance, config, facts):
        self.plan_calls += 1
        return parse_action({"name": "get_clinic_info", "arguments": {"topic": "services"}})

    def close(self):
        pass

    def drain(self, timeout):
        return True

    def forbidden(self, *args, **kwargs):
        self.external_attempts += 1
        raise RuntimeError("External transport is disabled for the synthetic qualification.")

    def audit_socket(self, event, args):
        # libpq uses its fixed literal DSN, outside Python socket auditing.
        if event == "socket.getaddrinfo" and args[:2] != ("127.0.0.1", 56379):
            self.forbidden()
        if event == "socket.connect" and args[1] != ("127.0.0.1", 56379):
            self.forbidden()


def reset(db: Session, settings: Settings) -> None:
    require_database(db)
    for clinic in (INFO, BOOKING):
        row = db.get(Tenant, clinic.tenant)
        if row is not None:
            if row.name != clinic.name:
                raise Refusal("A reserved synthetic tenant identity belongs to another fixture.")
            db.execute(delete(Tenant).where(Tenant.id == clinic.tenant, Tenant.name == clinic.name))
    # Admission buckets are global; delete only the two known synthetic keys.
    for scope, subject in (("start_user", f"{INFO.tenant}:{INFO.user}"), ("start_ip", PEER)):
        db.execute(
            text("DELETE FROM admission_buckets WHERE scope=:scope AND bucket_key=:key"),
            {"scope": scope, "key": bucket_key(settings, scope, subject)},
        )


def seed(db: Session, profile: Profile) -> None:
    for clinic, count in ((INFO, 1), (BOOKING, profile.clients + 1)):
        db.add(Tenant(id=clinic.tenant, name=clinic.name, timezone="UTC"))
        db.flush()
        db.add_all(
            [
                User(
                    id=clinic.user,
                    tenant_id=clinic.tenant,
                    role="owner",
                    email=f"{clinic.label}@phase7.invalid",
                    password_hash="unused-synthetic-login",
                ),
                Service(
                    id=clinic.service,
                    tenant_id=clinic.tenant,
                    name="Synthetic consultation",
                    name_ur="مصنوعی مشورہ",
                    duration_min=30,
                ),
            ]
        )
        for index in range(count):
            db.add_all(
                [
                    Provider(
                        id=clinic.provider(index),
                        tenant_id=clinic.tenant,
                        name=f"Synthetic provider {index}",
                    ),
                    Patient(
                        id=clinic.patient(index),
                        tenant_id=clinic.tenant,
                        name=f"Synthetic patient {index}",
                        name_key=f"synthetic patient {index}",
                        phone=f"+15550100{index:03}",
                    ),
                ]
            )
        db.flush()
        for index in range(count):
            provider = clinic.provider(index)
            db.add(
                ProviderService(
                    tenant_id=clinic.tenant, provider_id=provider, service_id=clinic.service
                )
            )
            db.add_all(
                [
                    Schedule(
                        tenant_id=clinic.tenant,
                        provider_id=provider,
                        weekday=day,
                        start_time=time(9),
                        end_time=time(10, 30),
                        slot_min=30,
                    )
                    for day in range(1, 8)
                ]
            )
    agent = Agent(id=AGENT, tenant_id=INFO.tenant, name="Synthetic information agent")
    db.add(agent)
    db.flush()
    version = AgentVersion(
        tenant_id=INFO.tenant, agent_id=AGENT, config={"language_mode": "bilingual"}
    )
    db.add(version)
    db.flush()
    agent.active_version_id = version.id


@dataclass
class Sample:
    started: float
    entered: float | None = None
    finished: float = 0
    elapsed_ms: float = 0
    status: int | None = None
    failure: str | None = None
    admitted: bool = False


current_sample: ContextVar[Sample | None] = ContextVar("load_sample", default=None)


class Measurements:
    def __init__(self, app):
        self.app = app
        self.groups: dict[str, list[Sample]] = {}

    async def asgi(self, scope, receive, send):
        sample = current_sample.get()
        if sample is not None:
            sample.entered = perf_counter()
        await self.app(scope, receive, send)

    async def call(
        self,
        client: httpx.AsyncClient,
        group: str,
        method: str,
        path: str,
        check: Callable[[int, object], bool],
        **kwargs,
    ) -> tuple[int | None, dict]:
        sample = Sample(perf_counter())
        self.groups.setdefault(group, []).append(sample)
        mark = current_sample.set(sample)
        try:
            response = await asyncio.wait_for(client.request(method, path, **kwargs), timeout=10)
            sample.status = response.status_code
        except TimeoutError:
            sample.failure = "timeout"
            return None, {}
        except httpx.HTTPError:
            sample.failure = "transport_error"
            return None, {}
        finally:
            sample.finished = perf_counter()
            sample.elapsed_ms = (sample.finished - sample.started) * 1000
            current_sample.reset(mark)
        try:
            data = response.json()
            sample.admitted = sample.entered is not None and (
                200 <= sample.status < 300 or error_code(data) == "slot_unavailable"
            )
            if not check(sample.status, data):
                sample.failure = error_code(data) or "unexpected_response"
        except (ValueError, TypeError, AttributeError, KeyError):
            data = {}
            sample.failure = "unexpected_response"
        return sample.status, data

    def report(self) -> dict:
        report = {}
        for name, samples in self.groups.items():
            # The successful booking SLO measures verified 201s only. All other
            # timings are also preserved in the raw, status-labeled observations.
            valid = [s.elapsed_ms for s in samples if s.failure is None]
            report[name] = {
                "attempted": len(samples),
                "admitted": sum(s.admitted for s in samples),
                "peak_attempted_in_flight": peak([(s.started, s.finished) for s in samples]),
                "peak_app_in_flight": peak(
                    [(s.entered, s.finished) for s in samples if s.entered is not None]
                ),
                "peak_admitted_in_flight": peak(
                    [
                        (s.entered, s.finished)
                        for s in samples
                        if s.entered is not None and s.admitted
                    ]
                ),
                "statuses": dict(
                    Counter(str(s.status) if s.status is not None else "none" for s in samples)
                ),
                "failures": dict(Counter(s.failure for s in samples if s.failure is not None)),
                "p50_ms": rounded(quantile(valid, 0.50)),
                "p95_ms": rounded(quantile(valid, 0.95)),
                "p95_under_target": bool(valid and quantile(valid, 0.95) < TARGET_MS)
                if name in {"slot_search", "successful_booking"}
                else None,
                "samples": [
                    {"ms": round(s.elapsed_ms, 6), "status": s.status, "failure": s.failure}
                    for s in samples
                ],
            }
        return report


def rounded(value: float | None) -> float | None:
    return round(value, 6) if value is not None else None


def peak(intervals: list[tuple[float, float]]) -> int:
    events = sorted([(start, 1) for start, _ in intervals] + [(end, -1) for _, end in intervals])
    count = maximum = 0
    for _, change in events:
        count += change
        maximum = max(maximum, count)
    return maximum


def error_code(data) -> str | None:
    error = data.get("error") if isinstance(data, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, str) and code in ERROR_CODES else None


def exact_information_replay(original: dict, replay: dict) -> bool:
    if not isinstance(original.get("state"), dict):
        return False
    return replay == (original | {"state": recovery_checkpoint(original["state"])})


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def exercise(app, profile, settings, state_ids):
    measure = Measurements(app)
    transport = httpx.ASGITransport(
        app=measure.asgi, client=(PEER, 45678), raise_app_exceptions=False
    )
    staff = bearer(access_token(INFO.user, INFO.tenant, settings))
    booking_staff = bearer(access_token(BOOKING.user, BOOKING.tenant, settings))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport, base_url="http://qualification.invalid", trust_env=False
        ) as client,
    ):

        async def start():
            response = await client.post(
                "/agent-sessions", headers=staff, json={"agent_id": str(AGENT)}
            )
            if response.status_code != 201 or response.json().get("cache_ready") is not True:
                raise Refusal(f"Synthetic runtime admission failed (HTTP {response.status_code}).")
            data = response.json()
            state_ids.append(data["session_id"])
            return data["session_id"], bearer(data["token"])

        async def search(day, group):
            params = {
                "service_id": str(INFO.service),
                "provider_id": str(INFO.provider(0)),
                "date_from": slot(day).date(),
                "date_to": slot(day).date(),
            }

            def check(status, data):
                return (
                    status == 200
                    and [datetime.fromisoformat(row["starts_at"]) for row in data]
                    == [slot(day, index) for index in range(3)]
                    and all(
                        row["provider_id"] == str(INFO.provider(0))
                        and row["service_id"] == str(INFO.service)
                        and datetime.fromisoformat(row["ends_at"])
                        - datetime.fromisoformat(row["starts_at"])
                        == timedelta(minutes=30)
                        for row in data
                    )
                )

            await measure.call(client, group, "GET", "/slots", check, params=params, headers=staff)

        async def information(runtime, revision, group, expected=None):
            sid, headers = runtime
            path = f"/runtime/sessions/{sid}/commands"
            command = {
                "command_id": f"phase7-info-{revision}",
                "expected_revision": revision,
                "action": {"name": "get_clinic_info", "arguments": {"topic": "services"}},
            }

            def check(status, data):
                return (
                    status == 200
                    and data["revision"] == revision + 1
                    and data["cache_ready"] is True
                    and data["provider_requests"] == 0
                    and data["result"]["ok"] is True
                    and [row["id"] for row in data["result"]["data"]["services"]]
                    == [str(INFO.service)]
                )

            _, data = await measure.call(
                client,
                group,
                "POST",
                path,
                lambda status, data: (
                    check(status, data)
                    and (expected is None or exact_information_replay(expected, data))
                ),
                headers=headers,
                json=command,
            )
            return data

        async def book(provider, patient, start_at, key, group, contention=False):
            body = {
                "provider_id": str(provider),
                "patient_id": str(patient),
                "service_id": str(BOOKING.service),
                "starts_at": start_at.isoformat(),
                "confirmed": True,
            }
            headers = {**booking_staff, "Idempotency-Key": key}

            def check(status, data):
                if contention and status == 409:
                    return error_code(data) == "slot_unavailable"
                return (
                    status == 201
                    and data["patient_id"] == str(patient)
                    and data["provider_id"] == str(provider)
                    and data["service_id"] == str(BOOKING.service)
                    and datetime.fromisoformat(data["starts_at"]) == start_at
                    and data["status"] == "booked"
                )

            return await measure.call(
                client, group, "POST", "/appointments", check, headers=headers, json=body
            )

        await search(1, "warmup_search")
        warm = await start()
        warm_original = await information(warm, 0, "warmup_information")
        await information(warm, 0, "warmup_replay", warm_original)
        await book(
            BOOKING.provider(0), BOOKING.patient(0), slot(1), "phase7-warmup", "warmup_booking"
        )

        # Barriers separate rounds, so every client offers one call at a time.
        for index in range(profile.rounds):
            await asyncio.gather(
                *(search(2 + index // 3, "slot_search") for _ in range(profile.clients))
            )

        runtimes = [await start() for _ in range(profile.clients)]
        # Keep original and replay waves separate, with the identical command payload.
        for index in range(profile.rounds):
            originals = await asyncio.gather(
                *(information(runtime, index, "information_original") for runtime in runtimes)
            )
            await asyncio.gather(
                *(
                    information(runtime, index, "information_replay", original)
                    for runtime, original in zip(runtimes, originals, strict=True)
                )
            )

        for index in range(profile.rounds):
            await asyncio.gather(
                *(
                    book(
                        BOOKING.provider(i),
                        BOOKING.patient(i),
                        slot(2 + index // 3, index % 3),
                        f"phase7-book-{i}-{index}",
                        "successful_booking",
                    )
                    for i in range(profile.clients)
                )
            )
        provider = BOOKING.provider(profile.clients)
        race = await asyncio.gather(
            *(
                book(
                    provider,
                    BOOKING.patient(i),
                    slot(12),
                    f"phase7-race-{i}",
                    "slot_contention",
                    True,
                )
                for i in range(profile.clients)
            )
        )
        winners = [(i, row) for i, (status, row) in enumerate(race) if status == 201]
        race_replay = False
        winner_index = None
        if len(winners) == 1:
            winner_index, winner = winners[0]
            status, replay = await book(
                provider,
                BOOKING.patient(winner_index),
                slot(12),
                f"phase7-race-{winner_index}",
                "contention_winner_replay",
            )
            race_replay = status == 201 and winner == replay
        return measure.report(), winner_index, race_replay


def persisted(db: Session, profile: Profile, winner: int | None) -> dict:
    appointments = db.execute(
        select(Appointment.provider_id, Appointment.patient_id, Appointment.starts_at).where(
            Appointment.tenant_id == BOOKING.tenant
        )
    ).all()
    expected = {(BOOKING.provider(0), BOOKING.patient(0), slot(1))}
    expected |= {
        (BOOKING.provider(i), BOOKING.patient(i), slot(2 + index // 3, index % 3))
        for i in range(profile.clients)
        for index in range(profile.rounds)
    }
    if winner is not None:
        expected.add((BOOKING.provider(profile.clients), BOOKING.patient(winner), slot(12)))
    keys = db.scalars(select(Idempotency.key).where(Idempotency.tenant_id == BOOKING.tenant)).all()
    expected_keys = {"phase7-warmup"} | {
        f"phase7-book-{i}-{index}"
        for i in range(profile.clients)
        for index in range(profile.rounds)
    }
    if winner is not None:
        expected_keys.add(f"phase7-race-{winner}")
    revisions = db.scalars(
        select(AgentSession.revision).where(AgentSession.tenant_id == INFO.tenant)
    ).all()
    receipt_count = len(
        db.scalars(select(CommandReceipt.id).where(CommandReceipt.tenant_id == INFO.tenant)).all()
    )
    return {
        "appointment_rows": len(appointments),
        "booking_receipts": len(keys),
        "command_receipts": receipt_count,
        "information_sessions": len(revisions),
        "exact_appointment_set_and_count": set(appointments) == expected
        and len(appointments) == len(expected),
        "exact_booking_receipts": set(keys) == expected_keys,
        "exact_information_revisions": sorted(revisions)
        == [1, *([profile.rounds] * profile.clients)],
        "exact_command_receipt_count": receipt_count == 1 + profile.clients * profile.rounds,
    }


def qualification_result(profile: Profile, groups: dict, *, correctness: bool) -> bool | None:
    if not profile.qualifying:
        return None
    if not correctness:
        return False
    for name in (
        "slot_search",
        "information_original",
        "information_replay",
        "successful_booking",
        "slot_contention",
    ):
        group = groups.get(name)
        if not isinstance(group, dict):
            return False
        admitted_peak = group.get("peak_admitted_in_flight")
        if type(admitted_peak) is not int or admitted_peak != profile.clients:
            return False
    return all(
        groups[name].get("p95_under_target") is True
        for name in ("slot_search", "successful_booking")
    )


def run(profile: Profile) -> dict:
    context = source_context()
    settings = Settings(
        database_url=FIXTURE_ENV["TEST_APP_DATABASE_URL"],
        valkey_url=FIXTURE_ENV["TEST_VALKEY_URL"],
        jwt_secret="phase7-load-synthetic-staff-admission-secret",
        runtime_secret="phase7-load-synthetic-runtime-capability-secret",
        _env_file=None,
    )
    adapters = OfflineAdapters()
    sys.addaudithook(adapters.audit_socket)
    admin = create_engine(
        FIXTURE_ENV["TEST_DATABASE_URL"], hide_parameters=True, connect_args={"connect_timeout": 3}
    )
    state_ids: list[str] = []
    initialized = False
    try:
        with (
            patch("scheduler.main.Nemotron", return_value=adapters),
            patch.object(draft_tests, "sweep", return_value=0),
            patch.object(voice_routes, "LiveKitRooms", adapters.forbidden),
            patch.object(httpx.HTTPTransport, "handle_request", adapters.forbidden),
            patch.object(httpx.AsyncHTTPTransport, "handle_async_request", adapters.forbidden),
        ):
            app = create_app(settings, clock=lambda: NOW)
            store = app.state.brain_store
            store.prefix = "phase7-disposable-load"
            with app.state.engine.connect() as connection:
                require_database(connection, schema=False)
            with admin.connect() as connection:
                require_database(connection)
                database_version = connection.scalar(text("SHOW server_version_num"))
            if not store.client.ping():
                raise Refusal("The dedicated Valkey fixture did not respond.")
            valkey_version = store.client.info("server").get("valkey_version")
            with Session(admin) as db, db.begin():
                reset(db, settings)
                seed(db, profile)
            initialized = True
            groups, winner, race_replay = asyncio.run(exercise(app, profile, settings, state_ids))
            with Session(admin) as db:
                sql = persisted(db, profile, winner)
    finally:
        try:
            if initialized:
                try:
                    for sid in state_ids:
                        store.delete(sid)
                finally:
                    store.client.close()
                    with Session(admin) as db, db.begin():
                        reset(db, settings)
        finally:
            admin.dispose()
    expected_count = profile.clients * profile.rounds
    counts = all(
        groups[name]["statuses"] == {status: expected_count}
        for name, status in (
            ("slot_search", "200"),
            ("information_original", "200"),
            ("information_replay", "200"),
            ("successful_booking", "201"),
        )
    )
    race_statuses = {"201": 1}
    if profile.clients > 1:
        race_statuses["409"] = profile.clients - 1
    correctness = (
        counts
        and groups["slot_contention"]["statuses"] == race_statuses
        and race_replay
        and not any(group["failures"] for group in groups.values())
        and all(value for value in sql.values() if isinstance(value, bool))
        and adapters.plan_calls == adapters.external_attempts == 0
    )
    return {
        "schema": SCHEMA,
        "runtime": context
        | {"postgres_server_version_num": database_version, "valkey": valkey_version},
        "profile": {
            "clients": profile.clients,
            "rounds": profile.rounds,
            "p95_strictly_below_ms": TARGET_MS,
            "clock_utc": NOW.isoformat(),
            "quantile": "linear interpolation at (n - 1) * q",
            "transport": "in-process HTTP ASGI",
            "per_request_timeout_seconds": 10,
            "synthetic_tenants": 2,
            "booking_slots_per_day": 3,
            "booking_days": 10,
        },
        "workloads": groups,
        "correctness": correctness,
        "qualification": qualification_result(profile, groups, correctness=correctness),
        "persistence": sql | {"race_winner_replay_exact": race_replay},
        "adapters": {
            "planner_calls": adapters.plan_calls,
            "external_attempts": adapters.external_attempts,
        },
    }
