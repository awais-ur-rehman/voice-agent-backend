"""Valkey live state and session-owned soft holds; SQL supplies security fencing."""

import json
import math
from typing import Any, cast
from uuid import uuid4

from valkey import Valkey

_STATE_WRITE = """
local raw = redis.call('GET', KEYS[1])
local incoming = cjson.decode(ARGV[1])
if ARGV[4] == 'repair' then
  if raw and cjson.decode(raw).revision >= incoming.revision then return 0 end
elseif ARGV[2] == 'new' then
  if raw then return 0 end
else
  if not raw or cjson.decode(raw).revision ~= tonumber(ARGV[2]) then return 0 end
end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[3])
return 1
"""

_HOLD_PRUNE = """
local clock = redis.call('TIME')
local now = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
local entries = redis.call('HGETALL', KEYS[1])
local live = {}
for i=1,#entries,2 do
  local hold = cjson.decode(entries[i+1])
  if hold.expires_at_ms <= now then
    redis.call('HDEL', KEYS[1], entries[i])
  else
    table.insert(live, hold)
  end
end
"""
_HOLD_ACQUIRE = (
    _HOLD_PRUNE
    + """
local request = cjson.decode(ARGV[1])
for _,hold in ipairs(live) do
  if hold.session == request.session and hold.offer_ref == request.offer_ref then
    if hold.start_epoch ~= request.start_epoch or hold.end_epoch ~= request.end_epoch then
      return false
    end
    return cjson.encode(hold)
  end
  if hold.start_epoch < request.end_epoch and request.start_epoch < hold.end_epoch then
    return false
  end
end
request.expires_at_ms = now + tonumber(ARGV[2])
redis.call('HSET', KEYS[1], request.hold_id, cjson.encode(request))
-- Never shorten another live hold's cleanup lifetime.
local latest = request.expires_at_ms
for _,hold in ipairs(live) do latest = math.max(latest, hold.expires_at_ms) end
redis.call('PEXPIREAT', KEYS[1], latest)
return cjson.encode(request)
"""
)
_HOLD_LIST = (
    _HOLD_PRUNE
    + """
local encoded = {}
for _,hold in ipairs(live) do table.insert(encoded, cjson.encode(hold)) end
return encoded
"""
)
_HOLD_RELEASE = (
    _HOLD_PRUNE
    + """
for _,hold in ipairs(live) do
  if hold.hold_id == ARGV[1] and hold.session == ARGV[2] then
    return redis.call('HDEL', KEYS[1], ARGV[1])
  end
end
return 0
"""
)


def _bounded_ttl(value: int, maximum: int) -> None:
    if type(value) is not int or not 0 < value <= maximum:
        raise ValueError(f"TTL must be an integer between 1 and {maximum}")


def _revision(state: dict[str, Any]) -> int:
    revision = state.get("revision")
    if type(revision) is not int or revision < 0:
        raise ValueError("State revision must be a nonnegative integer")
    return revision


class StateStore:
    def __init__(self, client: Valkey, prefix: str = "brain") -> None:
        self.client = client
        self.prefix = prefix

    def _key(self, kind: str, *parts: str) -> str:
        # JSON tuples avoid cross-tenant collisions from delimiters in identifiers.
        return f"{self.prefix}:{kind}:{json.dumps(parts, separators=(',', ':'))}"

    def load(self, session_id: str) -> dict[str, Any] | None:
        raw = self.client.get(self._key("state", session_id))
        return json.loads(cast(str | bytes, raw)) if raw is not None else None

    def put(
        self,
        session_id: str,
        state: dict[str, Any],
        expected_revision: int | None,
        ttl_seconds: int = 1800,
    ) -> bool:
        revision = _revision(state)
        if expected_revision is None:
            if revision != 0:
                raise ValueError("New state must have revision zero")
        elif type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("Expected revision must be nonnegative")
        elif revision != expected_revision + 1:
            raise ValueError("State must advance exactly one revision")
        return self._write(session_id, state, expected_revision, ttl_seconds, "cas")

    def repair(self, session_id: str, state: dict[str, Any], ttl_seconds: int = 1800) -> bool:
        """Repair only from a live, authoritative SQL guard checked by the caller."""
        _revision(state)
        return self._write(session_id, state, None, ttl_seconds, "repair")

    def _write(
        self,
        session_id: str,
        state: dict[str, Any],
        expected: int | None,
        ttl: int,
        mode: str,
    ) -> bool:
        _bounded_ttl(ttl, 1800)
        return bool(
            self.client.eval(
                _STATE_WRITE,
                1,
                self._key("state", session_id),
                json.dumps(state, allow_nan=False),
                "new" if expected is None else str(expected),
                str(ttl),
                mode,
            )
        )

    def delete(self, session_id: str) -> None:
        self.client.delete(self._key("state", session_id))

    def acquire_hold(
        self,
        tenant: str,
        provider: str,
        session: str,
        offer_ref: str,
        start_epoch: float,
        end_epoch: float,
        ttl_ms: int = 120000,
    ) -> dict[str, Any] | None:
        _bounded_ttl(ttl_ms, 120000)
        if (
            isinstance(start_epoch, bool)
            or isinstance(end_epoch, bool)
            or not math.isfinite(start_epoch)
            or not math.isfinite(end_epoch)
            or start_epoch >= end_epoch
        ):
            raise ValueError("Hold range must be finite and increasing")
        request = {
            "hold_id": uuid4().hex,
            "tenant": tenant,
            "provider": provider,
            "session": session,
            "offer_ref": offer_ref,
            "start_epoch": start_epoch,
            "end_epoch": end_epoch,
        }
        raw = self.client.eval(
            _HOLD_ACQUIRE,
            1,
            self._key("holds", tenant, provider),
            json.dumps(request, allow_nan=False),
            str(ttl_ms),
        )
        return json.loads(cast(str | bytes, raw)) if raw else None

    def holds(self, tenant: str, provider: str) -> list[dict[str, Any]]:
        rows = self.client.eval(_HOLD_LIST, 1, self._key("holds", tenant, provider))
        return [json.loads(row) for row in cast(list[str | bytes], rows)]

    def release_hold(self, tenant: str, provider: str, session: str, hold_id: str) -> bool:
        return bool(
            self.client.eval(
                _HOLD_RELEASE,
                1,
                self._key("holds", tenant, provider),
                hold_id,
                session,
            )
        )
