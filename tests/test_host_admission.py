"""Host admission: a heavy item never starts on a starving host, and a deferral
is a requeue with backoff -- never a failure, never a run."""

from __future__ import annotations

import json

import pytest
from awrun import dispatcher, host_admission, placement_budget
from awrun.dispatcher import dispatch_once
from awrun.store import STATUS_FAILED, STATUS_QUEUED, RunStore

CALM = {"memory_full_avg60_pct": 0.5, "cpu_some_avg60_pct": 5.0, "load_per_cpu": 1.0,
        "mem_available_gb": 30.0}
STARVING = {**CALM, "memory_full_avg60_pct": 84.6, "load_per_cpu": 21.2,
            "mem_available_gb": 9.0}


@pytest.fixture(autouse=True)
def _on(monkeypatch, tmp_path):
    monkeypatch.delenv("AWRUN_HOST_ADMISSION", raising=False)
    monkeypatch.delenv("AWRUN_HOST_BUDGET", raising=False)
    # The machine running the tests may itself be a fleet node: never read its state.
    monkeypatch.setenv("AITHER_PLACEMENT_DIR", str(tmp_path / "not-a-placement-node"))
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


# ── placement nodes: the memory floor is raised, never lowered ──────────────
# _fake_proc reports MemAvailable = 32 GB and calm pressure, so only the memory
# floor can refuse in the tests below.

def _node(tmp_path, headroom_mb=6144, manifest=None, raw=None, name="placement"):
    """A joined node's on-disk placement state under tmp_path/<name>."""
    root = tmp_path / name
    root.mkdir()
    (root / "agent.env").write_text(
        "AITHER_NODE_ID=test-node\n"
        f"AITHER_PLACEMENT_HEADROOM_MB={headroom_mb}\n", encoding="utf-8")
    if raw is not None:
        (root / "desired.json").write_text(raw, encoding="utf-8")
    elif manifest is not None:
        (root / "desired.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def _units(*mem_mb):
    return {"present": [{"unit": f"svc{i}.service", "mem_mb": mb}
                        for i, mb in enumerate(mem_mb)]}


def _set_mem_available_gb(proc, gb):
    (proc / "meminfo").write_text(
        f"MemAvailable:   {gb * 1024 * 1024} kB\n", encoding="utf-8")


@pytest.fixture
def inactive(monkeypatch):
    """Every placed unit reads as not running; records what was asked."""
    asked: list = []

    def _fn(units):
        asked.append(list(units))
        return set()

    monkeypatch.setattr(placement_budget, "systemctl_active", _fn)
    return asked


def test_not_joined_host_verdict_is_unchanged(tmp_path):
    proc = _fake_proc(tmp_path, 0.5)
    empty = tmp_path / "empty"
    empty.mkdir()
    before = host_admission.admit("ci", proc=proc)
    assert before == (True, "within host budget", host_admission.sample(proc))
    # Declared memory and an explicit non-node directory change nothing at all.
    assert host_admission.admit("ci", proc=proc, memory_mb=30000,
                                placement_root=empty) == before
    assert host_admission.admit("agent", proc=proc, memory_mb=30000,
                                placement_root=empty) == (
        True, "agent is not a heavy kind", None)


def test_joined_node_raises_floor_by_headroom_plus_pending_plus_job(tmp_path, inactive):
    proc = _fake_proc(tmp_path, 0.5)
    assert host_admission.admit("ci", proc=proc)[0] is True  # admitted before
    root = _node(tmp_path, headroom_mb=6144, manifest=_units(8192, 10240))
    # 6 GB headroom + 18 GB placed-not-running + 10 GB job = 34 GB > 32 GB available.
    ok, why, now = host_admission.admit("ci", proc=proc, memory_mb=10240,
                                        placement_root=root)
    assert ok is False and now["mem_available_gb"] == 32.0
    assert why == ("MemAvailable 32.0 GB < 34 GB (placement floor: headroom 6.0 GB"
                   " + placed units not running 18.0 GB + this job 10.0 GB)")
    assert inactive == [["svc0.service", "svc1.service"]], "ONE question for all units"
    # One unit fewer and the same job fits: 6 + 8 + 10 = 24 GB.
    smaller = _node(tmp_path, manifest=_units(8192), name="smaller")
    assert host_admission.admit("ci", proc=proc, memory_mb=10240,
                                placement_root=smaller)[:2] == (True, "within host budget")


def test_lower_placement_floor_is_ignored(tmp_path, inactive):
    proc = _fake_proc(tmp_path, 0.5)
    root = _node(tmp_path, headroom_mb=1024, manifest=_units(1024))
    # 1 + 1 + 1 = 3 GB is below the 12 GB default: the default stays in force.
    _set_mem_available_gb(proc, 10)
    ok, why, _ = host_admission.admit("ci", proc=proc, memory_mb=1024, placement_root=root)
    assert ok is False and why == "MemAvailable 10.0 GB < 12 GB"


def test_env_override_cannot_lower_below_placement_floor(tmp_path, inactive, monkeypatch):
    proc = _fake_proc(tmp_path, 0.5)
    root = _node(tmp_path, headroom_mb=6144, manifest=_units(30720))
    monkeypatch.setenv("AWRUN_HOST_BUDGET", '{"mem_available_min_gb": 1}')
    assert host_admission.budget()["mem_available_min_gb"] == 1.0
    ok, why, _ = host_admission.admit("ci", proc=proc, placement_root=root)
    assert ok is False and "MemAvailable 32.0 GB < 36 GB" in why
    # The same override on a host that is not a node still applies as before.
    assert host_admission.admit("ci", proc=proc)[0] is True


def test_advisory_manifest_adds_no_pending(tmp_path, inactive):
    proc = _fake_proc(tmp_path, 0.5)
    manifest = dict(_units(30720, 30720), advisory=True)
    root = _node(tmp_path, headroom_mb=6144, manifest=manifest)
    ok, why, _ = host_admission.admit("ci", proc=proc, memory_mb=1024, placement_root=root)
    assert (ok, why) == (True, "within host budget")
    assert inactive == [], "an advisory manifest asks systemd nothing"


def test_active_units_are_not_counted_twice(tmp_path, monkeypatch):
    proc = _fake_proc(tmp_path, 0.5)
    root = _node(tmp_path, headroom_mb=6144, manifest=_units(20480, 4096))
    # svc0 (20 GB) is running: it is already inside MemAvailable.
    monkeypatch.setattr(placement_budget, "systemctl_active",
                        lambda units: {"svc0.service"})
    ok, why, _ = host_admission.admit("ci", proc=proc, memory_mb=10240, placement_root=root)
    assert (ok, why) == (True, "within host budget")  # 6 + 4 + 10 = 20 GB
    monkeypatch.setattr(placement_budget, "systemctl_active", lambda units: set())
    assert host_admission.admit("ci", proc=proc, memory_mb=10240,
                                placement_root=root)[0] is False  # 6 + 24 + 10 = 40 GB


def test_systemctl_failure_counts_all_present_and_says_so(tmp_path, monkeypatch):
    proc = _fake_proc(tmp_path, 0.5)
    root = _node(tmp_path, headroom_mb=6144, manifest=_units(20480, 10240))
    monkeypatch.setattr(placement_budget, "systemctl_active", lambda units: None)
    ok, why, _ = host_admission.admit("ci", proc=proc, placement_root=root)
    assert ok is False and "MemAvailable 32.0 GB < 36 GB" in why
    assert placement_budget.NOTE_UNASKED in why


@pytest.mark.parametrize("raw", ["{not json", "[1, 2]", '{"present": "all"}'])
def test_unreadable_manifest_admits_on_base_floor_with_note(tmp_path, inactive, raw):
    proc = _fake_proc(tmp_path, 0.5)
    root = _node(tmp_path, headroom_mb=6144, raw=raw)
    ok, why, _ = host_admission.admit("ci", proc=proc, placement_root=root)
    assert ok is True, "an unreadable manifest must never block every job"
    assert why == f"within host budget [{placement_budget.NOTE_UNREADABLE}]"
    # ... and the base floor still refuses a host that is short of memory.
    _set_mem_available_gb(proc, 10)
    ok, why, _ = host_admission.admit("ci", proc=proc, placement_root=root)
    assert ok is False and why.startswith("MemAvailable 10.0 GB < 12 GB")
    assert placement_budget.NOTE_UNREADABLE in why


def test_light_kind_with_declared_memory_is_judged_on_a_joined_node_and_not_elsewhere(
        tmp_path, inactive):
    proc = _fake_proc(tmp_path, 0.5)
    root = _node(tmp_path, headroom_mb=6144, manifest=_units(20480))
    ok, why, now = host_admission.admit("agent", proc=proc, memory_mb=10240,
                                        placement_root=root)
    assert ok is False and now is not None and "this job 10.0 GB" in why
    # No declared memory: a light kind is not judged, even on a node.
    assert host_admission.admit("agent", proc=proc, placement_root=root) == (
        True, "agent is not a heavy kind", None)
    assert host_admission.admit("agent", proc=proc, memory_mb=0, placement_root=root) == (
        True, "agent is not a heavy kind", None)
    # Not a node: declared memory is not judged.
    assert host_admission.admit("agent", proc=proc, memory_mb=10240) == (
        True, "agent is not a heavy kind", None)


def test_admission_off_still_admits(tmp_path, inactive, monkeypatch):
    proc = _fake_proc(tmp_path, 99.0)
    root = _node(tmp_path, headroom_mb=6144, manifest=_units(102400))
    monkeypatch.setenv("AWRUN_HOST_ADMISSION", "off")
    assert host_admission.admit("ci", proc=proc, memory_mb=102400, placement_root=root) == (
        True, "host admission disabled (AWRUN_HOST_ADMISSION=off)", None)
    assert host_admission.admit("agent", proc=proc, memory_mb=102400,
                                placement_root=root) == (
        True, "agent is not a heavy kind", None)
    assert inactive == [], "off means nothing is read or asked"


def _enforcing(ran):
    def fn(i):
        ran.append(i.id)
        return 0, "ok"

    fn.awrun_enforces = ("memory_mb",)
    return fn


def test_dispatcher_requeues_with_placement_reason(tmp_path, monkeypatch, inactive):
    root = _node(tmp_path, headroom_mb=6144, manifest=_units(20480))
    monkeypatch.setenv("AITHER_PLACEMENT_DIR", str(root))
    monkeypatch.setattr(host_admission, "sample", lambda proc=None: dict(CALM))
    store = RunStore(tmp_path / "q")
    item = store.submit("ci", {"job": "x"}, limits={"memory_mb": 8192})
    ran: list = []
    back = dispatch_once(store, worker_id="w1", run_fns={"ci": _enforcing(ran)},
                         now_fn=lambda: 1000.0)
    assert back is not None and back.id == item.id
    assert back.status == STATUS_QUEUED and ran == []
    assert store.list(statuses=[STATUS_FAILED]) == []
    reason = (back.wait or {}).get("reason", "")
    # 6 GB headroom + 20 GB placed + 8 GB declared by the item = 34 GB > 30 GB.
    assert reason.startswith("host over budget: MemAvailable 30.0 GB < 34 GB")
    assert "placement floor" in reason and "this job 8.0 GB" in reason
    assert back.not_before > 1000.0


def test_dispatcher_runs_the_same_item_when_the_node_has_room(tmp_path, monkeypatch, inactive):
    root = _node(tmp_path, headroom_mb=6144, manifest=_units(4096))
    monkeypatch.setenv("AITHER_PLACEMENT_DIR", str(root))
    monkeypatch.setattr(host_admission, "sample", lambda proc=None: dict(CALM))
    store = RunStore(tmp_path / "q")
    store.submit("ci", {"job": "x"}, limits={"memory_mb": 8192})
    ran: list = []
    dispatch_once(store, worker_id="w1", run_fns={"ci": _enforcing(ran)},
                  now_fn=lambda: 1000.0)
    assert len(ran) == 1
