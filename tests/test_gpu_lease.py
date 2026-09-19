"""awrun.gpu_lease: the stdlib door client, exercised with an injected `post`
so no test opens a socket. The three outcomes must stay three DIFFERENT
things -- a 409 is an answer, a dead socket is not."""

from __future__ import annotations

import pytest
from awrun import gpu_lease
from awrun.gpu_lease import (
    Lease,
    LeaseRefused,
    LeaseUnavailable,
    acquire,
    heartbeat,
    release,
)

BASES = ["http://door-a", "http://door-b"]


def _refusing(url, body, timeout):
    return 409, {"detail": {"reason": "outranked", "need_mb": body["vram_mb"], "free_mb": 1,
                            "host": "gpu-0",
                            "lanes": {"wait": {"eta_s": 42}, "cloud_card": {"card_id": "d-9"}}}}


def test_200_with_token_is_a_lease_on_the_door_that_answered():
    calls: list = []

    def post(url, body, timeout):
        calls.append(url)
        return 200, {"token": "tok", "host": "gpu-0", "backend_url": "http://b",
                     "granted_mb": 18000, "expires_at": 1.5}

    lease = acquire("interactive_media", 18000, job_ref="awrun:r-1", door_bases=BASES, post=post)
    assert isinstance(lease, Lease)
    assert (lease.token, lease.host, lease.backend_url, lease.granted_mb) == (
        "tok", "gpu-0", "http://b", 18000)
    assert lease.door == "http://door-a"
    assert calls == ["http://door-a/acquire"]  # first door that answers wins


def test_409_is_a_refusal_carrying_lanes_card_and_retry_hint():
    with pytest.raises(LeaseRefused) as info:
        acquire("chat", 4000, door_bases=BASES, post=_refusing)
    exc = info.value
    assert exc.reason == "outranked"
    assert exc.card_id == "d-9"
    assert exc.busy_retry_ms == 42000
    record = exc.to_json()
    assert record["lease_refused"] is True and record["busyRetryMs"] == 42000
    assert record["lanes"]["cloud_card"]["card_id"] == "d-9"


def test_refusal_is_not_an_unavailability():
    assert not issubclass(LeaseRefused, LeaseUnavailable)
    assert not issubclass(LeaseUnavailable, LeaseRefused)


def test_every_door_dead_is_unavailable_and_names_each_door():
    def post(url, body, timeout):
        raise OSError("connection refused")

    with pytest.raises(LeaseUnavailable) as info:
        acquire("arc", 8000, door_bases=BASES, post=post)
    msg = str(info.value)
    assert "door-a" in msg and "door-b" in msg


def test_first_door_dead_second_answers():
    def post(url, body, timeout):
        if url.startswith("http://door-a"):
            raise OSError("down")
        return 200, {"token": "t2"}

    lease = acquire("arc", 8000, door_bases=BASES, post=post)
    assert lease.door == "http://door-b" and lease.token == "t2"


def test_no_door_configured_is_unavailable_naming_the_variable(monkeypatch):
    monkeypatch.delenv(gpu_lease._BASE_ENV, raising=False)
    with pytest.raises(LeaseUnavailable) as info:
        acquire("arc", 8000, post=lambda *a: (_ for _ in ()).throw(AssertionError("no post")))
    assert gpu_lease._BASE_ENV in str(info.value)


def test_bases_come_from_env_comma_separated(monkeypatch):
    monkeypatch.setenv(gpu_lease._BASE_ENV, " http://x/ , http://y ,, ")
    assert gpu_lease.bases() == ["http://x", "http://y"]


def test_unexpected_status_is_unavailable_not_a_lease():
    with pytest.raises(LeaseUnavailable):
        acquire("arc", 8000, door_bases=BASES[:1], post=lambda *a: (500, {"detail": "boom"}))


def test_release_and_heartbeat_without_a_token_say_false():
    empty = Lease()
    assert release(empty, "done", post=lambda *a: (200, {"released": True})) is False
    assert heartbeat(empty, post=lambda *a: (200, {})) is False


def test_release_reports_the_doors_confirmation():
    lease = Lease(token="tok", door="http://door-a")
    seen: list = []

    def post(url, body, timeout):
        seen.append((url, body))
        return 200, {"released": True}

    assert release(lease, "failed", post=post) is True
    assert seen == [("http://door-a/release", {"token": "tok", "outcome": "failed"})]
    assert release(lease, "done", post=lambda *a: (200, {"released": False})) is False
    assert release(lease, "done", post=lambda *a: (_ for _ in ()).throw(OSError("x"))) is False


def test_heartbeat_true_only_on_200():
    lease = Lease(token="tok", door="http://door-a")
    assert heartbeat(lease, post=lambda *a: (200, {})) is True
    assert heartbeat(lease, post=lambda *a: (404, {})) is False
    assert heartbeat(lease, post=lambda *a: (_ for _ in ()).throw(OSError("x"))) is False


def test_http_client_failures_are_unavailable_not_raw():
    """BadStatusLine / RemoteDisconnected are http.client.HTTPException, NOT
    OSError; a door that answers garbage must still read as unreachable."""
    import http.client

    def post(url, body, timeout):
        raise http.client.BadStatusLine("garbage")

    with pytest.raises(LeaseUnavailable) as info:
        acquire("arc", 8000, door_bases=BASES[:1], post=post)
    assert "BadStatusLine" in str(info.value)
    lease = Lease(token="tok", door="http://door-a")
    assert heartbeat(lease, post=post) is False
    assert release(lease, "done", post=post) is False
