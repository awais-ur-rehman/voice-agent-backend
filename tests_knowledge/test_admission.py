"""HTTP admission tests use real ASGI requests and blocked synthetic operations."""

import asyncio
import concurrent.futures
import threading
import time
from pathlib import Path

import httpx2

from embedding_service.server import create_app
from embedding_service.settings import Settings
from knowledge.profile import PROFILE

HEADERS = {"Authorization": "Bearer " + "s" * 32}
BODY = {"texts": ["synthetic"], "profile": PROFILE.fingerprint}


class BlockedWorker:
    ready = True

    def __init__(self):
        self.release = threading.Event()
        self.started = 0
        self.lock = threading.Lock()

    def start(self):
        pass

    def close(self):
        self.release.set()

    def embed(self, texts, *, deadline):
        with self.lock:
            self.started += 1
        self.release.wait(3)
        return [[1.0] + [0.0] * 1023]


async def wait_started(worker):
    deadline = time.monotonic() + 1
    while worker.started < 5 and time.monotonic() < deadline:
        await asyncio.sleep(0.005)
    assert worker.started == 5


def test_authenticated_route_admits_only_five_before_executor():
    async def run():
        asyncio.get_running_loop().set_default_executor(concurrent.futures.ThreadPoolExecutor(5))
        worker = BlockedWorker()
        app = create_app(Settings(Path("/unused"), "s" * 32, enabled=True), worker)
        async with app.router.lifespan_context(app):
            async with httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=app), base_url="http://private"
            ) as client:
                requests = [
                    asyncio.create_task(client.post("/internal/embed", headers=HEADERS, json=BODY))
                    for _ in range(12)
                ]
                try:
                    await wait_started(worker)
                    done, pending = await asyncio.wait(requests, timeout=0.1)
                    assert len(done) == 7, "overflow requests retained outside admission"
                    assert len(pending) == 5
                    assert all(task.result().status_code == 503 for task in done)
                    assert (await client.post("/internal/embed", json=BODY)).status_code == 401
                finally:
                    worker.release.set()
                    await asyncio.gather(*requests, return_exceptions=True)

    asyncio.run(run())


def test_cancelled_http_requests_retain_slots_until_worker_completion():
    async def run():
        asyncio.get_running_loop().set_default_executor(concurrent.futures.ThreadPoolExecutor(5))
        worker = BlockedWorker()
        app = create_app(Settings(Path("/unused"), "s" * 32, enabled=True), worker)
        async with app.router.lifespan_context(app):
            async with httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=app), base_url="http://private"
            ) as client:
                requests = [
                    asyncio.create_task(client.post("/internal/embed", headers=HEADERS, json=BODY))
                    for _ in range(5)
                ]
                overflow = None
                try:
                    await wait_started(worker)
                    for task in requests[:3]:
                        task.cancel()
                    await asyncio.gather(*requests[:3], return_exceptions=True)
                    overflow = asyncio.create_task(
                        client.post("/internal/embed", headers=HEADERS, json=BODY)
                    )
                    done, _ = await asyncio.wait([overflow], timeout=0.1)
                    assert done, "cancellation freed admission before operation finished"
                    assert overflow.result().status_code == 503
                    assert worker.started == 5
                    worker.release.set()
                    await asyncio.gather(*requests, return_exceptions=True)
                    # Executor completion, including abandoned requests, releases admission.
                    await asyncio.sleep(0.02)
                    assert (
                        await client.post("/internal/embed", headers=HEADERS, json=BODY)
                    ).status_code == 200
                finally:
                    worker.release.set()
                    await asyncio.gather(
                        *requests, *([overflow] if overflow else []), return_exceptions=True
                    )

    asyncio.run(run())


def test_slow_bodies_count_toward_admission_and_overflow_body_is_not_read():
    async def run():
        worker = BlockedWorker()
        app = create_app(Settings(Path("/unused"), "s" * 32, enabled=True), worker)
        body_started = 0
        release = asyncio.Event()

        async def body():
            nonlocal body_started
            body_started += 1
            await release.wait()
            yield b"{}"

        async with app.router.lifespan_context(app):
            async with httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=app), base_url="http://private"
            ) as client:
                requests = [
                    asyncio.create_task(
                        client.post("/internal/embed", headers=HEADERS, content=body())
                    )
                    for _ in range(5)
                ]
                overflow = None
                try:
                    for _ in range(100):
                        if body_started == 5:
                            break
                        await asyncio.sleep(0.005)
                    assert body_started == 5
                    overflow = asyncio.create_task(
                        client.post("/internal/embed", headers=HEADERS, content=body())
                    )
                    done, _ = await asyncio.wait([overflow], timeout=0.1)
                    assert done, "overflow body retained before admission"
                    assert overflow.result().status_code == 503
                    assert body_started == 5
                    # Cancellation during body receipt releases the slot immediately.
                    requests[0].cancel()
                    await asyncio.gather(requests[0], return_exceptions=True)
                    assert (
                        await client.post("/internal/embed", headers=HEADERS, json={})
                    ).status_code == 422
                finally:
                    release.set()
                    await asyncio.gather(
                        *requests, *([overflow] if overflow else []), return_exceptions=True
                    )

    asyncio.run(run())
