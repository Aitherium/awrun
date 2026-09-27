"""Host admission: a heavy item never starts on a starving host, and a deferral
is a requeue with backoff -- never a failure, never a run."""

from __future__ import annotations

import pytest
from awrun import dispatcher, host_admission
from awrun.dispatcher import dispatch_once
from awrun.store import STATUS_FAILED, STATUS_QUEUED, RunStore

CALM = {"memory_full_avg60_pct": 0.5, "cpu_some_avg60_pct": 5.0, "load_per_cpu": 1.0,
        "mem_available_gb": 30.0}
STARVING = {**CALM, "memory_full_avg60_pct": 84.6, "load_per_cpu": 21.2,
            "mem_available_gb": 9.0}


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.delenv("AWRUN_HOST_ADMISSION", raising=False)
    monkeypatch.delenv("AWRUN_HOST_BUDGET", raising=False)
    monkeypatch.setattr(dispatcher, "_relay_client", lambda: None)


def _fake_proc(tmp_path, mem_full: float):
    (tmp_path / "pressure").mkdir()
    (tmp_path / "pressure" / "memory").write_text(
        f"some avg10=0 avg60=1.00 avg300=0 total=1\n"
        f"full avg10=0 avg60={mem_full:.2f} avg300=0 total=1\n", encoding="utf-8")
    (tmp_path / "pressure" / "cpu").write_text(
        "some avg10=0 avg60=2.00 avg300=0 total=1\n", encoding="utf-8")
    (tmp_path / "loadavg").write_text("1.0 1.0 1.0 1/1 1\n", encoding="utf-8")
    (tmp_path / "meminfo").write_text("MemAvailable:   33554432 kB\n", encoding="utf-8")
    return tmp_path


def test_decide_calm_and_starving():
    assert host_admission.decide(CALM, host_admission.budget())[0] is True
    ok, why = host_admission.decide(STARVING, host_admission.budget())
    assert ok is False and "memory_full_avg60_pct" in why


def test_admit_reads_proc(tmp_path):
    ok, why, now = host_admission.admit("ci", proc=_fake_proc(tmp_path, 45.0))
    assert ok is False and now["memory_full_avg60_pct"] == 45.0


def test_light_kind_always_admitted(tmp_path):
    ok, why, _ = host_admission.admit("agent", proc=_fake_proc(tmp_path, 99.0))
    assert ok is True and "not a heavy kind" in why


def test_unreadable_host_is_admitted_and_says_so(tmp_path):
    ok, why, now = host_admission.admit("ci", proc=tmp_path / "missing")
    assert ok is True and "unjudged" in why and now is None


def test_budget_override_and_malformed(monkeypatch):
    monkeypatch.setenv("AWRUN_HOST_BUDGET", '{"memory_full_avg60_pct": 90}')
    assert host_admission.budget()["memory_full_avg60_pct"] == 90.0
    monkeypatch.setenv("AWRUN_HOST_BUDGET", "not json")
    assert host_admission.budget() == host_admission.DEFAULT_BUDGET


def test_dispatch_requeues_heavy_item_on_starving_host(tmp_path, monkeypatch):
    store = RunStore(tmp_path / "q")
    item = store.submit("ci", {"job": "x"})
    monkeypatch.setattr(host_admission, "sample", lambda proc=None: dict(STARVING))
    ran: list = []

    def fn(i):
        ran.append(i.id)
        return 0, "ok"

    back = dispatch_once(store, worker_id="w1", run_fns={"ci": fn}, now_fn=lambda: 1000.0)
    assert back is not None and back.id == item.id
    assert back.status == STATUS_QUEUED and ran == []
    assert store.list(statuses=[STATUS_FAILED]) == []
    assert "host over budget" in (back.wait or {}).get("reason", "")
    assert back.not_before > 1000.0


def test_dispatch_runs_heavy_item_on_calm_host(tmp_path, monkeypatch):
    store = RunStore(tmp_path / "q")
    store.submit("ci", {"job": "x"})
    monkeypatch.setattr(host_admission, "sample", lambda proc=None: dict(CALM))
    ran: list = []
    dispatch_once(store, worker_id="w1",
                  run_fns={"ci": lambda i: (ran.append(i.id), (0, "ok"))[1]},
                  now_fn=lambda: 1000.0)
    assert len(ran) == 1
