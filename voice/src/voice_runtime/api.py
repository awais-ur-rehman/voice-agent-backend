"""Server-only scoped worker API. Tokens remain in memory, never in logs."""

import asyncio
import uuid

import httpx


class WorkerAPI:
    def __init__(self, base_url, grant, *, client=None):
        self.grant = grant
        self.session_id = str(grant["session_id"])
        self.client = client or httpx.AsyncClient(
            base_url=base_url,
            timeout=190,
            headers={
                "Authorization": "Bearer " + grant["token"],
                "X-Runtime-Capability": grant["runtime_token"],
            },
        )
        self.revision = 0

    async def request(self, route, body=None, *, timeout_seconds=None):
        url = f"/voice/worker/sessions/{self.session_id}" + route
        options = {"timeout": timeout_seconds} if timeout_seconds is not None else {}
        response = (
            await self.client.get(url, **options)
            if body is None
            else await self.client.post(url, json=body, **options)
        )
        response.raise_for_status()
        result = response.json()
        if isinstance(result, dict) and "revision" in result:
            if type(result["revision"]) is not int or result["revision"] < 0:
                raise ValueError("invalid server revision")
            self.revision = max(self.revision, result["revision"])
        return result

    async def turn(self, text, *, started_at, language, confidence, uncertain):
        # Reuse exactly one operation ID/payload for the single ambiguous network retry.
        payload = {
            "command_id": str(uuid.uuid4()),
            "expected_revision": self.revision,
            "text": text,
            "utterance_started_at": started_at.isoformat(),
            "language": language,
            "confidence": confidence,
            "uncertain": uncertain,
        }
        for attempt in range(2):
            try:
                return await self.request("/turn", payload)
            except (httpx.TransportError, httpx.TimeoutException):
                if attempt:
                    raise
                await asyncio.sleep(0.1)

    async def interrupt(self):
        payload = {"command_id": str(uuid.uuid4())}
        for attempt in range(2):
            try:
                return await self.request("/interrupt", payload, timeout_seconds=3)
            except (httpx.TransportError, httpx.TimeoutException):
                if attempt:
                    raise
                await asyncio.sleep(0.1)

    async def event(self, kind):
        return await self.request(
            "/events",
            {
                "command_id": str(uuid.uuid4()),
                "expected_revision": self.revision,
                "kind": kind,
                "delivered_text": "",
            },
        )

    async def close(self):
        await self.client.aclose()
