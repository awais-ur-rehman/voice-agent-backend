import asyncio
from unittest.mock import AsyncMock


def test_brain_ended_response_plays_goodbye_then_closes_media_and_never_listens():
    from voice_runtime.pipeline import NativeCall

    async def scenario():
        call = NativeCall.__new__(NativeCall)
        call.ending = False
        call.epoch = 2
        call.api = type("API", (), {"request": AsyncMock()})()
        call.worker = type("Worker", (), {"cancel": AsyncMock()})()
        call.speak = AsyncMock()
        response = {"state": {"ended": True}, "result": {"say": {"en": "Goodbye."}}}
        await call.complete_response(response, "en", 2)
        assert call.ending is True
        call.speak.assert_awaited_once_with("Goodbye.", "en", None, 2)
        call.api.request.assert_awaited_once_with("/end", {})
        call.worker.cancel.assert_awaited_once()

    asyncio.run(scenario())
