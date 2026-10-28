import asyncio
from datetime import UTC, datetime

import httpx

from voice_runtime.api import WorkerAPI


def test_ambiguous_turn_retry_reuses_command_and_payload():
    requests = []

    def respond(request):
        requests.append(request.content)
        if len(requests) == 1:
            raise httpx.ReadError("ambiguous response")
        return httpx.Response(200, json={"revision": 3, "state": {}, "result": {}})

    async def scenario():
        client = httpx.AsyncClient(base_url="http://local", transport=httpx.MockTransport(respond))
        api = WorkerAPI(
            "http://local",
            {"session_id": "sid", "token": "test", "runtime_token": "test"},
            client=client,
        )
        await api.turn(
            "synthetic hello",
            started_at=datetime.now(UTC),
            language="en",
            confidence=0.9,
            uncertain=False,
        )
        assert api.revision == 3
        assert requests[0] == requests[1]
        await api.close()

    asyncio.run(scenario())


def test_delayed_reply_never_rolls_worker_revision_back():
    async def scenario():
        replies = iter([{"revision": 3}, {"revision": 1}])
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json=next(replies)))
        client = httpx.AsyncClient(base_url="http://test", transport=transport)
        worker = WorkerAPI("http://test", {"session_id": "synthetic"}, client=client)
        await worker.request("")
        await worker.request("")
        assert worker.revision == 3
        await worker.close()

    asyncio.run(scenario())
