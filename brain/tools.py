"""Guarded tools executed only under the SQL session revision lock."""

import hashlib
import json
from copy import deepcopy
from datetime import date, datetime, time, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from pydantic import ValidationError
from sqlalchemy import Time, cast, func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from valkey.exceptions import ValkeyError

from scheduler.appointments import (
    AppointmentOut,
    Book,
    Confirm,
    Move,
    book_transaction,
    cancel_transaction,
    idempotent,
    locks,
    reschedule_transaction,
)
from scheduler.contracts import PatientCreate
from scheduler.errors import AppError
from scheduler.models import Appointment, Patient, Provider, Schedule, Service, Tenant
from scheduler.slots import get, search

from .contracts import parse_action
from .dialogue import affirmative, allowed_next, remember
from .templates import result


class ToolEngine:
    def __init__(self, db, actor, store, now, *, clock=None):
        self.db, self.actor, self.store, self.now = db, actor, store, now
        self.clock = clock or (lambda: self.now)

    def release(self, state):
        hold = state.pop("hold", None)
        if hold:
            self.store.release_hold(
                str(self.actor.tenant_id), hold["provider"], str(self.actor.id), hold["hold_id"]
            )
        state["readback"] = None

    def execute(self, state, raw, caller_text="", *, _retry=False):
        try:
            action = parse_action(raw)
        except ValidationError:
            return result(error="validation_error", field="action")
        name, args = action.name, action.arguments
        if name not in allowed_next(state):
            return result(error="not_verified" if not state.get("verified") else "validation_error")
        before = deepcopy(state)
        try:
            # Business failures roll back all DB effects, including partial reschedules.
            with self.db.begin_nested():
                response = self._execute(state, name, args, caller_text)
        except (AppError, ValidationError, ValueError) as exc:
            code = getattr(exc, "code", "validation_error")
            response = result(
                error=code
                if code
                in {
                    "not_verified",
                    "verification_failed",
                    "slot_unavailable",
                    "outside_policy",
                    "not_owner",
                    "rate_limited",
                    "no_answer",
                }
                else "validation_error"
            )
        except (ValkeyError, OperationalError) as exc:
            if isinstance(exc, OperationalError) and getattr(exc.orig, "sqlstate", None) not in {
                "57014",
                "55P03",
                "40P01",
                "40001",
            }:
                raise
            if isinstance(exc, OperationalError) and name in {
                "book_appointment",
                "reschedule_appointment",
                "cancel_appointment",
                "confirm",
            }:
                state.clear()
                state.update(before)
                if not _retry:
                    return self.execute(state, raw, caller_text, _retry=True)
                state["failures"] = max(1, state.get("failures", 0))
            state["failures"] = state.get("failures", 0) + 1
            state.pop("hold", None)
            state["readback"] = None
            if state["failures"] >= 2:
                self._callback(state, "technical")
                state["handoff"] = True
                response = result("handoff")
            else:
                response = result(error="upstream_timeout")
        except IntegrityError as exc:
            response = result(
                error="slot_unavailable"
                if getattr(exc.orig, "sqlstate", None) == "23P01"
                else "validation_error"
            )
        if response["ok"] and not state.get("handoff"):
            state["failures"] = 0
        state["silence_count"] = 0
        state["turns"] = state.get("turns", 0) + 1
        remember(
            state,
            caller_text,
            response["say"][state.get("lang", "en")],
            sensitive=name
            in {
                "find_patient",
                "verify_patient",
                "prepare_patient",
                "create_patient",
                "read_back",
                "find_appointments",
                "select_appointment",
                "book_appointment",
                "reschedule_appointment",
                "cancel_appointment",
                "confirm",
            },
        )
        return response

    def _execute(self, s, name, a, text):
        db = self.db
        tenant = get(db, Tenant, self.actor.tenant_id)
        if name == "ask_caller":
            return result(self.next_question(s))
        if name == "set_caller_contact":
            s["caller_contact"] = {"phone": PatientCreate.phone_number(a["phone"])}
            return result()
        if name == "update_dialogue_state":
            s["stage"] = "collect_need"
            if a.get("task") and a["task"] != s.get("task"):
                self.release(s)
                s.setdefault("parked", []).append(
                    {
                        "task": s.get("task", "book"),
                        "fields": s.get("fields", {}).copy(),
                        "subject": s.get("subject"),
                    }
                )
                s["parked"] = s["parked"][-3:]
                s["task"] = a["task"]
                s["fields"] = {}
                s.pop("appointment", None)
                s.pop("appointments", None)
            if a.get("subject"):
                self.release(s)
                for key in (
                    "candidates",
                    "subject",
                    "appointment",
                    "appointments",
                    "lookup_phone",
                    "pending_patient",
                ):
                    s.pop(key, None)
                s["verified"] = False
                s["stage"] = "identify"
                s["fields"] = {}
                s["offers"] = []
                # Subject switch is explicit; caller contact is deliberately unchanged.
            if s.get("task") == "reschedule" and s.get("appointment") and a.get("service_id"):
                original = get(db, Appointment, UUID(s["appointment"]))
                if a["service_id"] != str(original.service_id):
                    return result(error="validation_error", field="service_id")
            if s.get("task") == "cancel" and any(
                k in a for k in ("date_phrase", "provider_id", "service_id")
            ):
                self.release(s)
                s.pop("appointment", None)
                s.pop("appointments", None)
            fields = s.setdefault("fields", {})
            if any(k in a for k in ("date_phrase", "service_id", "provider_id")):
                self.release(s)
                s["offers"] = []
            for key, model in [("service_id", Service), ("provider_id", Provider)]:
                if key in a:
                    row = get(db, model, UUID(a[key]))
                    if not row.active:
                        return result(error="validation_error", field=key)
                    fields[key] = a[key]
                    if s.get("task") == "cancel" and key == "service_id":
                        fields["source_service_id"] = a[key]
            if "date_phrase" in a:
                from scheduler.models import AgentVersion, ExceptionDay, ProviderService

                from .resolver import resolve

                # A rejected correction cannot silently reuse old temporal intent.
                # Keep known fields for clarification, but forbid offers until fixed.
                fields["temporal_pending"] = True
                version = get(db, AgentVersion, self.actor.version_id)
                prayer = version.config.get("prayer_break")
                resolved = resolve(
                    a["date_phrase"],
                    now=self.now,
                    timezone=tenant.timezone,
                    context="history" if s.get("task") == "cancel" else "booking",
                    prayer_break=prayer,
                )
                ambiguity = resolved.get("clarify") == "am_or_pm"
                if resolved.get("error") or resolved.get("clarify") and not ambiguity:
                    return result(error="validation_error", field="date_phrase")
                first = resolved.get("date", resolved.get("date_from"))
                last = resolved.get("date_to", first)
                if ambiguity and first is None:
                    # Resolver ambiguity intentionally omits a partly resolved date.
                    # Probe both meridiems only to recover the expressed calendar
                    # date; actual clinic intervals below decide interpretation.
                    for bounds in (["00:00", "12:00"], ["12:00", "24:00"]):
                        probe = resolve(
                            a["date_phrase"],
                            now=self.now,
                            timezone=tenant.timezone,
                            context="booking",
                            clinic_hours=bounds,
                            prayer_break=prayer,
                        )
                        if probe.get("date") or probe.get("date_from"):
                            first = probe.get("date", probe.get("date_from"))
                            last = probe.get("date_to", first)
                            break
                first = first or fields.get("date")
                last = last or fields.get("date_to") or first
                if first is None:
                    return result(error="validation_error", field="date_phrase")
                first_day, last_day = date.fromisoformat(first), date.fromisoformat(last)
                if not 0 <= (last_day - first_day).days <= 30:
                    return result(error="validation_error", field="date_phrase")
                providers = list(db.scalars(select(Provider).where(Provider.active.is_(True))))
                if fields.get("provider_id"):
                    providers = [p for p in providers if str(p.id) == fields["provider_id"]]
                if fields.get("service_id"):
                    service_id = UUID(fields["service_id"])
                    providers = [
                        p
                        for p in providers
                        if db.get(ProviderService, (p.id, service_id)) is not None
                    ]
                intervals = []
                for provider in providers:
                    rules = list(
                        db.scalars(select(Schedule).where(Schedule.provider_id == provider.id))
                    )
                    exceptions = list(
                        db.scalars(
                            select(ExceptionDay).where(
                                ExceptionDay.provider_id == provider.id,
                                ExceptionDay.start_date <= last_day,
                                ExceptionDay.end_date >= first_day,
                            )
                        )
                    )
                    for offset in range((last_day - first_day).days + 1):
                        day = first_day + timedelta(days=offset)
                        matching = [e for e in exceptions if e.start_date <= day <= e.end_date]
                        if any(e.kind == "closed" for e in matching):
                            continue
                        custom = [e for e in matching if e.kind == "custom"]
                        windows = (
                            [(e.start_time, e.end_time) for e in custom]
                            if custom
                            else [
                                (r.start_time, r.end_time)
                                for r in rules
                                if r.weekday == day.isoweekday()
                            ]
                        )
                        for start_time, end_time in windows:
                            if start_time is None or end_time is None:
                                continue
                            start, end = start_time.strftime("%H:%M"), end_time.strftime("%H:%M")
                            if prayer and day.isoweekday() == 5:
                                # Jummah break removes starts from a Friday's
                                # interval without bridging a split shift.
                                if start < min(end, prayer[0]):
                                    intervals.append((start, min(end, prayer[0])))
                                if max(start, prayer[1]) < end:
                                    intervals.append((max(start, prayer[1]), end))
                            else:
                                intervals.append((start, end))
                if ambiguity:
                    candidates = [
                        candidate
                        for candidate in resolved["candidates"]
                        if any(start <= candidate < end for start, end in intervals)
                    ]
                    if len(candidates) != 1:
                        return result(error="validation_error", field="date_phrase")
                    resolved = {"time": candidates[0]}
                exact = resolved.get("time")
                window = resolved.get("preferred_window")
                if exact and not any(start <= exact < end for start, end in intervals):
                    return result(error="validation_error", field="date_phrase")
                if window and not any(
                    max(start, window[0]) < min(end, window[1]) for start, end in intervals
                ):
                    return result(error="validation_error", field="date_phrase")
                fields["date_phrase"] = a["date_phrase"]
                fields["date"], fields["date_to"] = first, last
                # A date-only correction retains a previously collected time.
                # A fresh clock/window replaces all old temporal constraints.
                if exact or window or resolved.get("time_from") or resolved.get("time_to"):
                    fields["time_exact"] = exact
                    fields["time_from"] = window[0] if window else resolved.get("time_from")
                    fields["time_to"] = window[1] if window else resolved.get("time_to")
                fields.pop("temporal_pending", None)
            return result(self.next_question(s))
        if name == "find_patient":
            self.release(s)
            s.update(verified=False, stage="identify", offers=[])
            for key in ("subject", "appointment", "appointments", "pending_patient", "candidates"):
                s.pop(key, None)
            s.pop("lookup_phone", None)
            s["lookup_empty"] = False
            phone = PatientCreate.phone_number(a["phone"])
            rows = list(db.scalars(select(Patient).where(Patient.phone == phone)))
            s["lookup_phone"] = phone
            s["candidates"] = [str(p.id) for p in rows]
            s["lookup_empty"] = not rows
            return result(
                "factor" if rows else "new", {"matches": len(rows), "phone_suffix": phone[-2:]}
            )
        if name == "verify_patient":
            candidates = list(
                db.scalars(
                    select(Patient).where(
                        Patient.id.in_([UUID(v) for v in s["candidates"]]),
                        Patient.phone == s["lookup_phone"],
                    )
                )
            )
            factor = " ".join(a["factor"].casefold().split())
            matches = [
                p
                for p in candidates
                if factor == p.name_key or p.dob is not None and factor == p.dob.isoformat()
            ]
            if len(matches) != 1:
                s["attempts"] = s.get("attempts", 0) + 1
                if s["attempts"] >= 3:
                    s["handoff"] = True
                    self._callback(s, "verification_failed")
                return result(error="verification_failed")
            s["subject"] = str(matches[0].id)
            s["verified"] = True
            s["stage"] = "collect_need"
            s.pop("candidates", None)
            s.pop("lookup_phone", None)
            return result("verified")
        if name == "prepare_patient":
            self.release(s)
            s.pop("pending_patient", None)
            if PatientCreate.phone_number(a["phone"]) != s.get("lookup_phone"):
                return result(error="validation_error", field="phone")
            from .normalization import say_name, say_phone

            s["pending_patient"] = {k: v for k, v in a.items() if k != "dob"}
            if not a.get("name_ur"):
                return result(
                    error="validation_error",
                    field="name_ur",
                    say={
                        "en": "Please provide the reviewed Urdu spelling of the patient’s name.",
                        "ur": "براہ کرم مریض کے نام کا تصدیق شدہ اردو املا بتائیں۔",
                    },
                )
            say = {
                lang: f"{say_name(a['name'], a.get('name_ur'), lang=lang)}; {say_phone(a['phone'], lang=lang, read_digits_in_english=s.get('read_digits_in_english', False))}. "
                + ("Is this correct?" if lang == "en" else "کیا یہ درست ہے؟")
                for lang in ("en", "ur")
            }
            s["readback"] = {"kind": "patient", "revision": s["revision"] + 1, "say": say}
            return result(say=say)
        if (
            name == "create_patient"
            or name == "confirm"
            and s.get("readback", {}).get("kind") == "patient"
        ):
            if not self._confirmed(s, text, "patient"):
                return result(error="validation_error", field="confirmation")
            a = s.pop("pending_patient")
            row = Patient(
                tenant_id=self.actor.tenant_id,
                name=a["name"],
                name_key=" ".join(a["name"].casefold().split()),
                name_ur=a.get("name_ur"),
                phone=PatientCreate.phone_number(a["phone"]),
            )
            db.add(row)
            db.flush()
            s.update(
                subject=str(row.id),
                verified=True,
                stage="collect_need",
                readback=None,
                lookup_empty=False,
            )
            s.pop("lookup_phone", None)
            return result("patient_created")
        if name == "get_clinic_info":
            topic = a.get("topic", "services")
            if topic in ("services", "fees"):
                rows = list(db.scalars(select(Service).where(Service.active.is_(True))))
                from .normalization import say_currency, say_name

                say = {
                    lang: "; ".join(
                        f"{say_name(r.name, r.name_ur, lang=lang)}: {say_currency(r.price, lang=lang)}"
                        for r in rows
                    )
                    or result(error="no_answer")["say"][lang]
                    for lang in ("en", "ur")
                }
                return result(
                    data={
                        "services": [
                            {
                                "id": str(r.id),
                                "name": r.name,
                                "name_ur": r.name_ur,
                                "price": str(r.price),
                            }
                            for r in rows
                        ]
                    },
                    say=say,
                )
            if topic == "providers":
                rows = list(db.scalars(select(Provider).where(Provider.active.is_(True))))
                from .normalization import say_name

                return result(
                    data={
                        "providers": [
                            {"id": str(r.id), "name": r.name, "name_ur": r.name_ur} for r in rows
                        ]
                    },
                    say={
                        lang: "; ".join(say_name(r.name, r.name_ur, lang=lang) for r in rows)
                        or result(error="no_answer")["say"][lang]
                        for lang in ("en", "ur")
                    },
                )
            if topic in ("location", "preparation"):
                from scheduler.models import AgentVersion

                profile = get(db, AgentVersion, self.actor.version_id).config.get(
                    "clinic_profile", {}
                )
                prefix = "address" if topic == "location" else "preparation"
                if not all(profile.get(prefix + "_" + lang) for lang in ("en", "ur")):
                    return result(error="no_answer")
                return result(say={lang: profile[prefix + "_" + lang] for lang in ("en", "ur")})
            if topic == "hours":
                from .normalization import say_time

                rules = list(
                    db.scalars(select(Schedule).order_by(Schedule.weekday, Schedule.start_time))
                )
                if not rules:
                    return result(error="no_answer")
                return result(
                    data={
                        "hours": [
                            {
                                "weekday": r.weekday,
                                "start": r.start_time.isoformat(),
                                "end": r.end_time.isoformat(),
                            }
                            for r in rules
                        ]
                    },
                    say={
                        lang: "; ".join(
                            dict.fromkeys(
                                say_time(r.start_time, lang=lang)
                                + " – "
                                + say_time(r.end_time, lang=lang)
                                for r in rules
                            )
                        )
                        for lang in ("en", "ur")
                    },
                )
            if topic == "policy":
                from .normalization import number_en, number_ur

                def duration(value, lang):
                    number = number_en if lang == "en" else number_ur
                    units = [("day", "دن", 1440), ("hour", "گھنٹے", 60), ("minute", "منٹ", 1)]
                    parts = []
                    for en, ur, size in units:
                        count, value = divmod(value, size)
                        if count:
                            parts.append(
                                number(count)
                                + " "
                                + (en + ("s" if count != 1 else "") if lang == "en" else ur)
                            )
                    return ", ".join(parts) or number(0) + " " + (
                        "minutes" if lang == "en" else "منٹ"
                    )

                return result(
                    data={
                        "cancellation_min": tenant.cancellation_min,
                        "notice_min": tenant.min_notice_min,
                        "horizon_days": tenant.horizon_days,
                        "daily_limit": tenant.daily_limit,
                    },
                    say={
                        "en": "Booking notice: "
                        + duration(tenant.min_notice_min, "en")
                        + ". Cancellation or rescheduling notice: "
                        + duration(tenant.cancellation_min, "en")
                        + ".",
                        "ur": "بکنگ کی پیشگی مہلت: "
                        + duration(tenant.min_notice_min, "ur")
                        + "۔ منسوخی یا وقت کی تبدیلی کی پیشگی مہلت: "
                        + duration(tenant.cancellation_min, "ur")
                        + "۔",
                    },
                )
            return result(error="no_answer")
        if name == "search_knowledge":
            return result(error="no_answer")
        if name == "find_appointments":
            self.release(s)
            s.pop("appointment", None)
            s["stage"] = "choose_appointment"
            fields = s.get("fields", {})
            query = select(Appointment).where(
                Appointment.patient_id == UUID(s["subject"]),
                Appointment.status == "booked",
                Appointment.starts_at >= self.now,
            )
            if s.get("task") == "cancel":
                if fields.get("temporal_pending"):
                    return result(error="validation_error", field="date_phrase")
                if fields.get("date"):
                    first = date.fromisoformat(fields["date"])
                    last = date.fromisoformat(fields.get("date_to") or fields["date"])
                    zone = ZoneInfo(tenant.timezone)
                    query = query.where(
                        Appointment.starts_at >= datetime.combine(first, time.min, zone)
                    )
                    if last < date.max:
                        query = query.where(
                            Appointment.starts_at
                            < datetime.combine(last + timedelta(days=1), time.min, zone)
                        )
                if fields.get("provider_id"):
                    query = query.where(Appointment.provider_id == UUID(fields["provider_id"]))
                if fields.get("source_service_id"):
                    query = query.where(Appointment.service_id == UUID(fields["source_service_id"]))
                local_clock = cast(func.timezone(tenant.timezone, Appointment.starts_at), Time)
                if fields.get("time_exact"):
                    query = query.where(local_clock == time.fromisoformat(fields["time_exact"]))
                if fields.get("time_from"):
                    query = query.where(local_clock >= time.fromisoformat(fields["time_from"]))
                if fields.get("time_to") and fields["time_to"] != "24:00":
                    query = query.where(local_clock < time.fromisoformat(fields["time_to"]))
            rows = list(db.scalars(query.order_by(Appointment.starts_at).limit(20)))
            s["appointments"] = [
                {"ref": str(uuid4()), "id": str(r.id), "starts_at": r.starts_at.isoformat()}
                for r in rows
            ]
            from .normalization import say_date, say_time

            say = {
                lang: "; ".join(
                    say_date(r.starts_at.astimezone(ZoneInfo(tenant.timezone)).date(), lang=lang)
                    + " "
                    + say_time(r.starts_at.astimezone(ZoneInfo(tenant.timezone)).time(), lang=lang)
                    for r in rows
                )
                or result(error="no_answer")["say"][lang]
                for lang in ("en", "ur")
            }
            return result(
                data={
                    "appointments": [
                        {k: v for k, v in r.items() if k != "id"} for r in s["appointments"]
                    ]
                },
                say=say,
            )
        if name == "select_appointment":
            chosen = next((r for r in s["appointments"] if r["ref"] == a["ref"]), None)
            if not chosen:
                return result(error="not_owner")
            row = get(db, Appointment, UUID(chosen["id"]))
            if str(row.patient_id) != s["subject"]:
                return result(error="not_owner")
            self.release(s)
            s["appointment"] = chosen["id"]
            s["stage"] = "collect_need"
            s["fields"]["service_id"] = str(row.service_id)
            return result()
        if name == "get_available_slots":
            fields = s["fields"]
            if fields.get("temporal_pending"):
                return result(error="validation_error", field="date_phrase")
            from scheduler.models import AgentVersion

            version = get(db, AgentVersion, self.actor.version_id)
            prayer = version.config.get("prayer_break")
            service = get(db, Service, UUID(fields["service_id"]))
            rows = search(
                db,
                tenant,
                service.id,
                date.fromisoformat(fields["date"]),
                date.fromisoformat(fields.get("date_to") or fields["date"]),
                self.now,
                UUID(fields["provider_id"]) if fields.get("provider_id") else None,
                # Apply preferences before the three-offer cap. A pre-filter
                # search cap can hide a later matching day in the same range.
                limit=None,
            )
            offers = []
            for row in rows:
                local_start = row.starts_at.astimezone(ZoneInfo(tenant.timezone))
                local = local_start.strftime("%H:%M")
                if fields.get("time_exact") and local != fields["time_exact"]:
                    continue
                if prayer and local_start.isoweekday() == 5 and prayer[0] <= local < prayer[1]:
                    continue
                if (
                    fields.get("time_from")
                    and local < fields["time_from"]
                    or fields.get("time_to")
                    and local >= fields["time_to"]
                ):
                    continue
                start = (row.starts_at - timedelta(minutes=service.buffer_before_min)).timestamp()
                end = (row.ends_at + timedelta(minutes=service.buffer_after_min)).timestamp()
                if any(
                    h["session"] != str(self.actor.id)
                    and h["start_epoch"] < end
                    and start < h["end_epoch"]
                    for h in self.store.holds(str(self.actor.tenant_id), str(row.provider_id))
                ):
                    continue
                offers.append(
                    row.model_dump(mode="json")
                    | {"ref": str(uuid4()), "start_epoch": start, "end_epoch": end}
                )
                if len(offers) == 3:
                    break
            self.release(s)
            s["offers"] = offers
            s["stage"] = "choose_slot"
            if not offers:
                return result(error="slot_unavailable")
            from .normalization import say_date, say_time

            say = {
                lang: "; ".join(
                    say_date(
                        datetime.fromisoformat(o["starts_at"])
                        .astimezone(ZoneInfo(tenant.timezone))
                        .date(),
                        lang=lang,
                    )
                    + " "
                    + say_time(
                        datetime.fromisoformat(o["starts_at"])
                        .astimezone(ZoneInfo(tenant.timezone))
                        .time(),
                        lang=lang,
                    )
                    for o in offers
                )
                for lang in ("en", "ur")
            }
            for lang in ("en", "ur"):
                say[lang] += ". " + result("choose_time")["say"][lang]
            return result(
                data={"offers": [{"ref": o["ref"], "starts_at": o["starts_at"]} for o in offers]},
                say=say,
            )
        if name == "hold_slot":
            offer = next((o for o in s["offers"] if o["ref"] == a["ref"]), None)
            if not offer:
                return result(error="validation_error", field="ref")
            if s.get("hold") and s.get("selection", {}).get("ref") == offer["ref"]:
                return result("hold")
            self.release(s)
            hold = self.store.acquire_hold(
                str(self.actor.tenant_id),
                offer["provider_id"],
                str(self.actor.id),
                offer["ref"],
                offer["start_epoch"],
                offer["end_epoch"],
            )
            if not hold:
                return result(error="slot_unavailable")
            s["hold"] = hold
            s["stage"] = "read_back"
            s["selection"] = offer
            return result("hold")
        if name == "read_back":
            s["readback"] = None
            from .normalization import say_date, say_name, say_phone, say_time

            patient = get(db, Patient, UUID(s["subject"]))
            selection = s.get("selection")
            read_start = (
                datetime.fromisoformat(selection["starts_at"])
                if s["task"] != "cancel"
                else get(db, Appointment, UUID(s["appointment"])).starts_at
            )
            provider = get(
                db,
                Provider,
                UUID(selection["provider_id"])
                if s["task"] != "cancel"
                else get(db, Appointment, UUID(s["appointment"])).provider_id,
            )
            service = get(db, Service, UUID(s["fields"]["service_id"]))
            read_local = read_start.astimezone(ZoneInfo(tenant.timezone))
            say = {
                lang: ("Please confirm: " if lang == "en" else "براہ کرم تصدیق کریں: ")
                + (
                    "cancel"
                    if lang == "en" and s["task"] == "cancel"
                    else "منسوخی"
                    if lang == "ur" and s["task"] == "cancel"
                    else ""
                )
                + "; ".join(
                    [
                        say_name(patient.name, patient.name_ur, lang=lang),
                        say_phone(
                            patient.phone,
                            lang=lang,
                            read_digits_in_english=s.get("read_digits_in_english", False),
                        ),
                        say_name(provider.name, provider.name_ur, lang=lang),
                        say_name(service.name, service.name_ur, lang=lang),
                        say_date(read_local.date(), lang=lang),
                        say_time(read_local.time(), lang=lang),
                    ]
                )
                + ("?" if lang == "en" else "؟")
                for lang in ("en", "ur")
            }
            if s["task"] == "reschedule":
                original = get(db, Appointment, UUID(s["appointment"]))
                old_local = original.starts_at.astimezone(ZoneInfo(tenant.timezone))
                for lang in ("en", "ur"):
                    say[lang] = (
                        ("Original appointment: " if lang == "en" else "پہلی اپوائنٹمنٹ: ")
                        + say_date(old_local.date(), lang=lang)
                        + " "
                        + say_time(old_local.time(), lang=lang)
                        + ". "
                        + say[lang]
                    )
            s["readback"] = {
                "kind": "appointment",
                "revision": s["revision"] + 1,
                "say": say,
                "service_snapshot": [
                    service.name,
                    service.name_ur,
                    service.duration_min,
                    service.buffer_before_min,
                    service.buffer_after_min,
                ],
                "patient_snapshot": [patient.name, patient.name_ur, patient.phone],
                "provider_snapshot": [provider.name, provider.name_ur],
            }
            return result(say=say)
        if name in ("book_appointment", "cancel_appointment", "reschedule_appointment", "confirm"):
            if not self._confirmed(s, text, "appointment"):
                return result(error="validation_error", field="confirmation")
            if s["task"] == "book":
                locks(db, UUID(s["subject"]), {UUID(s["selection"]["provider_id"])})
            elif s["task"] == "reschedule":
                original = get(db, Appointment, UUID(s["appointment"]))
                locks(
                    db,
                    UUID(s["subject"]),
                    {original.provider_id, UUID(s["selection"]["provider_id"])},
                )
            elif s["task"] == "cancel":
                original = get(db, Appointment, UUID(s["appointment"]))
                locks(db, UUID(s["subject"]), {original.provider_id})
            self.now = self.clock()
            patient = get(db, Patient, UUID(s["subject"]))
            current_service = get(db, Service, UUID(s["fields"]["service_id"]))
            provider_id = (
                UUID(s["selection"]["provider_id"])
                if s["task"] != "cancel"
                else get(db, Appointment, UUID(s["appointment"])).provider_id
            )
            current_provider = get(db, Provider, provider_id)
            if (
                [
                    current_service.name,
                    current_service.name_ur,
                    current_service.duration_min,
                    current_service.buffer_before_min,
                    current_service.buffer_after_min,
                ]
                != s["readback"].get("service_snapshot")
                or [patient.name, patient.name_ur, patient.phone]
                != s["readback"].get("patient_snapshot")
                or [current_provider.name, current_provider.name_ur]
                != s["readback"].get("provider_snapshot")
            ):
                self.release(s)
                return result(error="slot_unavailable")
            if s["task"] != "cancel":
                hold = s.get("hold")
                if not hold or not any(
                    h["hold_id"] == hold["hold_id"] and h["session"] == str(self.actor.id)
                    for h in self.store.holds(str(self.actor.tenant_id), hold["provider"])
                ):
                    self.release(s)
                    return result(error="slot_unavailable")
                o = s["selection"]
                mutation_start = datetime.fromisoformat(o["starts_at"])
                if (
                    mutation_start - timedelta(minutes=current_service.buffer_before_min)
                ).timestamp() != hold["start_epoch"] or (
                    mutation_start
                    + timedelta(
                        minutes=current_service.duration_min + current_service.buffer_after_min
                    )
                ).timestamp() != hold["end_epoch"]:
                    self.release(s)
                    return result(error="slot_unavailable")
            if s["task"] == "book":
                body = Book(
                    patient_id=UUID(s["subject"]),
                    provider_id=UUID(o["provider_id"]),
                    service_id=UUID(o["service_id"]),
                    starts_at=mutation_start,
                    confirmed=True,
                )
                row = self._commit(
                    s, body, lambda: book_transaction(db, self.actor, body, self.now)
                )
            else:
                old = get(db, Appointment, UUID(s["appointment"]))
                locks(db, old.patient_id, {old.provider_id})
                self.now = self.clock()
                if str(old.patient_id) != s["subject"]:
                    return result(error="not_owner")
                if s["task"] == "cancel":
                    row = self._commit(
                        s,
                        Confirm(confirmed=True),
                        lambda: cancel_transaction(db, self.actor, old.id, self.now),
                    )
                else:
                    move = Move(
                        provider_id=UUID(o["provider_id"]), starts_at=mutation_start, confirmed=True
                    )
                    row = self._commit(
                        s,
                        move,
                        lambda: reschedule_transaction(db, self.actor, old.id, move, self.now),
                    )
            response = result(
                "cancelled" if s["task"] == "cancel" else "confirmed",
                {"appointment": AppointmentOut.model_validate(row).model_dump(mode="json")},
                say={
                    lang: result("cancelled" if s["task"] == "cancel" else "confirmed")["say"][lang]
                    + " "
                    + s["readback"]["say"][lang].split(": ", 1)[-1].rstrip("?؟")
                    for lang in ("en", "ur")
                },
            )
            self.release(s)
            s["offers"] = []
            s.pop("appointment", None)
            s.pop("appointments", None)
            s["stage"] = "complete"
            return response
        if name == "resume_task":
            if not s.get("parked"):
                return result(error="validation_error")
            self.release(s)
            old = s["parked"].pop()
            s["task"] = old["task"]
            s["fields"] = old["fields"]
            s["offers"] = []
            if old.get("subject") != s.get("subject"):
                s["verified"] = False
                s.pop("subject", None)
            s.pop("appointment", None)
            s.pop("appointments", None)
            return result(self.next_question(s))
        if name == "request_handoff":
            self.release(s)
            self._callback(s, a.get("reason", "requested"))
            s["handoff"] = True
            return result("emergency" if a.get("reason") == "emergency" else "handoff")
        if name == "end_call":
            self.release(s)
            s["ended"] = True
            return result("end")
        return result(error="validation_error")

    @staticmethod
    def next_question(state):
        if not state.get("verified"):
            return "factor" if state.get("candidates") else "phone"
        if state.get("fields", {}).get("temporal_pending"):
            return "date"
        if state.get("task") in ("cancel", "reschedule") and not state.get("appointment"):
            return "find_ready"
        if not state.get("fields", {}).get("service_id"):
            return "service"
        if not state.get("fields", {}).get("date"):
            return "date"
        return "search_ready"

    def _commit(self, state, body, execute):
        parameters = {
            "session": str(self.actor.id),
            "task": state["task"],
            "confirmation_revision": state["readback"]["revision"],
            "subject": state["subject"],
            "appointment": state.get("appointment"),
            "body": body.model_dump(mode="json"),
        }
        key = (
            "runtime_"
            + hashlib.sha256(
                json.dumps(parameters, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        )
        return idempotent(self.db, self.actor, key, "runtime:" + state["task"], body, execute)

    def trusted_event(self, state, kind, *, delivered_text=""):
        if kind == "delivery_interrupted":
            # Trusted transport input still may contain names. Keep only delivery metadata.
            state["readback"] = None
            state["history"] = [
                h for h in state.get("history", []) if h.get("role") != "assistant"
            ][-9:]
            state["history"].append({"role": "assistant", "content": "[delivery interrupted]"})
            return result("ask")
        if kind == "silence":
            state["silence_count"] = state.get("silence_count", 0) + 1
            if state["silence_count"] >= 3:
                self.release(state)
                state["ended"] = True
                return result("end")
            return result(
                "silence", data={"retry_after_seconds": 6 if state["silence_count"] == 1 else 12}
            )
        if kind == "upstream_timeout":
            state["failures"] = state.get("failures", 0) + 1
            if state["failures"] >= 2:
                self.release(state)
                self._callback(state, "technical")
                state["handoff"] = True
                return result("handoff")
            return result(error="upstream_timeout")
        return result(error="validation_error")

    def _confirmed(self, s, text, kind):
        readback = s.get("readback")
        return bool(
            readback
            and readback["kind"] == kind
            and readback["revision"] <= s["revision"]
            and affirmative(text)
        )

    def _callback(self, state, reason):
        from scheduler.models import Callback

        # Runtime actor/session attribution; no external calls or messages.
        self.db.add(
            Callback(
                tenant_id=self.actor.tenant_id,
                session_id=self.actor.id,
                agent_id=self.actor.agent_id,
                phone=state.get("caller_contact", {}).get("phone"),
                reason=reason,
            )
        )
