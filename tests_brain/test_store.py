"""Real Valkey regression tests; cleanup is restricted to a random namespace."""

import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
import valkey

from brain.store import StateStore


@pytest.fixture
def store():
    client = valkey.Valkey.from_url(
        "valkey://:phase2_valkey_test_only@127.0.0.1:56379/0", decode_responses=True
    )
    client.ping()
    prefix = f"test-brain-{uuid4().hex}"
    yield StateStore(client, prefix=prefix)
    for key in client.scan_iter(match=f"{prefix}:*"):
        client.delete(key)
    client.close()


def test_state_creation_cas_repair_and_delete(store):
    assert store.load("s") is None
    assert store.put("s", {"revision": 0, "task": "book"}, expected_revision=None)
    assert not store.put("s", {"revision": 0}, expected_revision=None)
    assert not store.put("s", {"revision": 2}, expected_revision=1)
    assert store.put("s", {"revision": 1, "task": "cancel"}, expected_revision=0)
    assert not store.repair("s", {"revision": 0})
    assert not store.repair("s", {"revision": 1, "task": "stale"})
    assert store.repair("s", {"revision": 3, "task": "book"})
    assert store.load("s") == {"revision": 3, "task": "book"}
    store.delete("s")
    assert store.load("s") is None


def test_expired_state_can_be_repaired(store):
    assert store.put("s", {"revision": 0}, expected_revision=None, ttl_seconds=1)
    time.sleep(1.05)
    assert store.load("s") is None
    assert store.repair("s", {"revision": 4})
    assert store.load("s") == {"revision": 4}


def test_competing_state_updates_have_one_winner(store):
    assert store.put("s", {"revision": 0}, expected_revision=None)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: store.put("s", {"revision": 1}, 0), range(8)))
    assert results.count(True) == 1


def test_overlapping_ranges_conflict_but_adjacent_and_other_providers_do_not(store):
    hold = store.acquire_hold("t", "p", "s1", "o1", 100, 200)
    assert hold is not None
    assert store.acquire_hold("t", "p", "s2", "o2", 150, 250) is None
    assert store.acquire_hold("t", "p", "s1", "o3", 150, 250) is None
    assert store.acquire_hold("t", "p", "s2", "o2", 200, 250) is not None
    assert store.acquire_hold("t", "p2", "s2", "o2", 150, 250) is not None
    assert store.acquire_hold("t2", "p", "s2", "o2", 150, 250) is not None
    assert len(store.holds("t", "p")) == 2
    assert not store.release_hold("t", "p", "s2", hold["hold_id"])
    assert not store.release_hold("t2", "p", "s1", hold["hold_id"])
    assert store.release_hold("t", "p", "s1", hold["hold_id"])
    assert not store.release_hold("t", "p", "s1", hold["hold_id"])


def test_duplicate_hold_does_not_extend_expiry_and_expired_hold_is_pruned(store):
    first = store.acquire_hold("t", "p", "s", "o", 100, 200, ttl_ms=80)
    assert first is not None
    duplicate = store.acquire_hold("t", "p", "s", "o", 100, 200)
    assert duplicate == first
    assert store.acquire_hold("t", "p", "s", "o", 110, 210) is None
    time.sleep(0.1)
    assert store.holds("t", "p") == []
    assert store.acquire_hold("t", "p", "other", "new", 150, 250) is not None


def test_simultaneous_overlapping_hold_requests_have_one_winner(store):
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda n: store.acquire_hold("t", "p", f"s{n}", f"o{n}", 100 + n, 200 + n),
                range(8),
            )
        )
    assert sum(item is not None for item in results) == 1


@pytest.mark.parametrize(
    "start,end,ttl",
    [
        (2, 1, 100),
        (1, 1, 100),
        (float("nan"), 2, 100),
        (1, float("inf"), 100),
        (1, 2, 0),
        (1, 2, 120001),
    ],
)
def test_invalid_hold_ranges_and_ttls_fail_before_writing(store, start, end, ttl):
    with pytest.raises(ValueError):
        store.acquire_hold("t", "p", "s", "o", start, end, ttl_ms=ttl)
    assert store.holds("t", "p") == []


@pytest.mark.parametrize(
    "state,expected,ttl",
    [
        ({"revision": 0}, None, 0),
        ({"revision": 0}, None, 1801),
        ({"revision": 2}, 0, 100),
        ({"revision": -1}, None, 100),
        ({"revision": True}, None, 100),
    ],
)
def test_invalid_state_revisions_and_ttls_are_rejected(store, state, expected, ttl):
    with pytest.raises(ValueError):
        store.put("s", state, expected, ttl_seconds=ttl)
    assert store.load("s") is None
