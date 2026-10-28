"""Bounded tenant-scoped cleanup after the media worker or browser disappears.

The private Valkey registry stores routing identifiers only. SQL state, ownership
and generation decide whether cleanup is due; cached fields cannot revive a call.
"""

import asyncio
import json
import logging
from types import SimpleNamespace
from uuid import UUID

from scheduler.db import Principal
from scheduler.errors import AppError

PREFIX = "voice:cleanup:"


def track(store, tenant_id, session_id, creator_id):
    store.client.set(
        PREFIX + str(session_id),
        json.dumps(
            {
                "tenant_id": str(tenant_id),
                "session_id": str(session_id),
                "creator_id": str(creator_id),
            }
        ),
        ex=86400,
    )


def sweep(app) -> int:
    from .voice_routes import _expired, _owned_pair, _terminate

    store = app.state.brain_store
    count = 0
    request = SimpleNamespace(app=app)
    for index, key in enumerate(store.client.scan_iter(match=PREFIX + "*", count=50)):
        if index >= 50:
            break
        raw = store.client.get(key)
        try:
            if raw is None:
                continue
            if len(raw) > 512:
                raise ValueError("registry")
            record = json.loads(raw)
            identity = UUID(record["session_id"])
            if key != PREFIX + str(identity):
                raise ValueError("registry")
            user = Principal(UUID(record["creator_id"]), UUID(record["tenant_id"]), "", "cleanup")
        except (ValueError, KeyError, TypeError):
            store.client.delete(key)
            continue
        try:
            with app.state.session_factory() as db, db.begin():
                parent, voice = _owned_pair(db, user, identity)
                due = voice.status in {"ended", "failed"} or _expired(
                    parent, voice, app.state.clock()
                )
                generation = voice.generation
                status = "failed" if voice.status == "provisioning" else "ended"
            if due:
                result = _terminate(
                    request, user, identity, status=status, expected_generation=generation
                )
                if result is not None:
                    count += 1
        except AppError as exc:
            if exc.code == "not_found":
                store.client.delete(key)
            else:
                raise
    return count


async def cleanup_loop(app, stop):
    failed = False
    while not stop.is_set():
        try:
            await asyncio.to_thread(sweep, app)
            failed = False
        except Exception:
            # One structured warning per failure streak; no identifying exception text.
            if not failed:
                logging.getLogger("scheduler.voice").warning('{"event":"voice_cleanup_retry"}')
                failed = True
        try:
            await asyncio.wait_for(stop.wait(), timeout=1)
        except TimeoutError:
            pass
