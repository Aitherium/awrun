"""Suspend/resume, apply and confinement, end to end.

The flow test starts a real child process, stops it mid-workflow through the
queue, and resumes it: the proof is the per-step call count on disk, which is
the only thing that says whether finished work was done twice.
"""

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest
from awrun import cli, dispatcher
from awrun.store import RunStore

HERE = Path(__file__).resolve()
AWFLOW_DIR = HERE.parents[2] / "awflow"

FLOW = '''
import asyncio, os
from pathlib import Path
from awflow import agent

CALLS = Path(os.environ["AWRUN_TEST_CALLS"])
STEPS = ["alpha", "beta", "gamma", "delta"]

async def _fake(prompt, *, model=None, schema=None, temperature=0.7, seed=None,
                max_tokens=2048, effort=None):
    with open(CALLS, "a", encoding="utf-8") as fh:
        fh.write(prompt + "\\n")
    if prompt == "do gamma" and not (CALLS.parent / "second-pass").exists():
        await asyncio.sleep(120)          # the step the suspend lands in
    return f"answer:{prompt}", 5, None

async def main():
    return [await agent(f"do {s}", label=s) for s in STEPS]

main.awrun_dispatcher = _fake
'''


class _NoRelay:
    """Tests never dial a relay: an absent client would fall back to the host's
    real one, bearer and all."""

    def send_text(self, **_kw):
        return None


def _dispatch(store, worker_id, **kw):
    return dispatcher.dispatch_once(store, worker_id=worker_id, relay_client=_NoRelay(), **kw)


def _calls(path: Path) -> list:
    return path.read_text(encoding="utf-8").split("\n")[:-1] if path.exists() else []


@pytest.mark.skipif(not (AWFLOW_DIR / "awflow" / "__init__.py").is_file(),
                    reason="the workflow engine is not alongside this package")
def test_a_suspended_flow_resumes_without_redoing_finished_calls(tmp_path, monkeypatch):
    script = tmp_path / "flow_under_test.py"
    script.write_text(FLOW, encoding="utf-8")
    calls = tmp_path / "calls.log"
    monkeypatch.setenv("AWRUN_TEST_CALLS", str(calls))
    monkeypatch.setenv("AWRUN_FLOW_FAKE_DISPATCHER", "1")
    monkeypatch.setenv("AITHER_AWFLOW_MIRROR", "0")
    paths = [str(AWFLOW_DIR), str(HERE.parents[1]), os.environ.get("PYTHONPATH", "")]
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(p for p in paths if p))
    monkeypatch.setattr(dispatcher, "_POLL_S", 0.1)

    store = RunStore(tmp_path / "q")
    item = store.submit("flow", {"script": str(script)}, priority=3, name="the-flow",
                        lineage={"goal": "G-42", "expedition": "exp-7"})
    box = {}
    worker = threading.Thread(
        target=lambda: box.update(out=_dispatch(store, "w1")))
    worker.start()

    deadline = time.time() + 60
    while "do gamma" not in _calls(calls) and time.time() < deadline:
        time.sleep(0.1)
    assert _calls(calls) == ["do alpha", "do beta", "do gamma"], _calls(calls)

    assert store.suspend(item.id).status == "running"     # a request, not yet a state
    worker.join(timeout=60)
    parked = box["out"]
    assert parked is not None and parked.status == "suspended", parked
    assert parked.checkpoint["kind"] == "flow-journal"
    assert parked.priority == 3 and parked.created_at == item.created_at
    assert store.claim_next(worker_id="w2") is None        # parked is not claimable
    assert not store.suspend_requested(item.id)

    (tmp_path / "second-pass").write_text("", encoding="utf-8")
    assert store.resume(item.id).resumes == 1
    done = _dispatch(store, "w2")
    assert done is not None and done.status == "done", done.result
    assert done.id == item.id and done.lineage == {"goal": "G-42", "expedition": "exp-7"}

    made = _calls(calls)
    assert made.count("do alpha") == 1 and made.count("do beta") == 1, made
    assert made.count("do delta") == 1, made
    # gamma was in flight when the run was stopped -- never journaled, so it is
    # made again. That is the honest cost of stopping mid-call: one call, not four.
    assert made.count("do gamma") == 2, made
    result = json.loads(done.result["message"].strip().splitlines()[-1])["result"]
    assert result == [f"answer:do {s}" for s in ("alpha", "beta", "gamma", "delta")]


def test_a_run_whose_limits_cannot_be_enforced_is_failed_not_run(tmp_path):
    store = RunStore(tmp_path)
    ran = []
    item = store.submit("agent", {"agent": "a", "task": "t"}, limits={"memory_mb": 256})

    def run_agent(it):
        ran.append(it.id)
        return 0, "ran unconfined"

    out = _dispatch(store, "w", run_fns={"agent": run_agent})
    # A host-registered runner that declares nothing enforces nothing.
    assert out.status == "failed" and "refusing to run unconfined" in out.result["message"]
    assert ran == [] and out.id == item.id


def test_timeout_is_enforced_on_a_real_child(tmp_path, monkeypatch):
    monkeypatch.setattr(dispatcher, "_POLL_S", 0.05)
    store = RunStore(tmp_path)
    store.submit("agent", {"agent": "a", "task": "t"}, limits={"timeout_s": 1})

    def run_agent(it):
        return dispatcher._run_child(
            it, [sys.executable, "-c", "import time; time.sleep(60)"], default_timeout=3600)
    run_agent.awrun_enforces = frozenset({"timeout_s"})

    began = time.time()
    out = _dispatch(store, "w", run_fns={"agent": run_agent})
    assert out.status == "failed" and "timed out after 1s" in out.result["message"]
    assert time.time() - began < 30


def _manifest(tmp_path, docs) -> str:
    path = tmp_path / "m.json"
    path.write_text(json.dumps(docs), encoding="utf-8")
    return str(path)


def _doc(name, priority=1, task="t", **spec):
    return {"apiVersion": "awrun/v1", "kind": "Run",
            "metadata": {"name": name, "lineage": {"goal": "G-1"}},
            "spec": {"kind": "agent", "priority": priority,
                     "run": {"agent": "a", "task": task}, **spec}}


def _apply(store, path, dry_run=False):
    return cli.cmd_apply(argparse.Namespace(file=path, dry_run=dry_run, json=True), store)


def test_apply_converges_instead_of_duplicating(tmp_path, capsys):
    store = RunStore(tmp_path / "q")
    path = _manifest(tmp_path, [_doc("one"), _doc("two", priority=5)])
    assert _apply(store, path) == 0
    assert [r["verdict"] for r in json.loads(capsys.readouterr().out)] == ["created"] * 2

    assert _apply(store, path) == 0
    assert [r["verdict"] for r in json.loads(capsys.readouterr().out)] == ["unchanged"] * 2
    assert len(store.list(statuses=["queued"])) == 2

    path = _manifest(tmp_path, [_doc("one", priority=9), _doc("two", priority=5, task="new")])
    assert _apply(store, path) == 0
    assert [r["verdict"] for r in json.loads(capsys.readouterr().out)] == \
        ["configured", "replaced"]
    queued = store.list(statuses=["queued"])
    assert [(i.name, i.priority, i.spec["task"]) for i in queued] == \
        [("one", 9, "t"), ("two", 5, "new")]


def test_apply_writes_nothing_when_any_document_is_bad(tmp_path):
    store = RunStore(tmp_path / "q")
    bad = _doc("two")
    bad["spec"]["limits"] = {"cpus": -1}
    assert _apply(store, _manifest(tmp_path, [_doc("one"), bad])) == 2
    assert store.list() == []


def test_apply_cannot_submit_what_submit_refuses(tmp_path, monkeypatch):
    for key in ("AITHER_SESSION_BEARER", "AWRUN_TUNNEL_OPERATORS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AWRUN_AUDIT_LOG", str(tmp_path / "audit.log"))
    store = RunStore(tmp_path / "q")
    doc = {"apiVersion": "awrun/v1", "kind": "Run", "metadata": {"name": "open-a-door"},
           "spec": {"kind": "tunnel", "run": {"action": "expose", "hostname": "x.example.com",
                                              "origin": "http://svc:80", "plane": "tunnel"}}}
    assert _apply(store, _manifest(tmp_path, [doc])) == 1
    assert store.list() == []


def test_dry_run_writes_nothing(tmp_path):
    store = RunStore(tmp_path / "q")
    assert _apply(store, _manifest(tmp_path, [_doc("one")]), dry_run=True) == 0
    assert store.list() == []


def test_a_journal_altered_while_parked_is_not_replayed(tmp_path):
    from awrun.handoff import sha256_file
    store = RunStore(tmp_path / "q")
    item = store.submit("flow", {"entry": "pkg.mod:main"})
    journal = store.path / "journals" / item.id / "journal.jsonl"
    journal.parent.mkdir(parents=True)
    journal.write_text('{"type":"AGENT_CALL","response":"real"}\n', encoding="utf-8")
    store.claim(item.id, worker_id="w")
    store.start(item.id)
    store.park(item.id, {"kind": "flow-journal", "journal": str(store.path / "journals"),
                         "journal_sha256": sha256_file(journal)})
    journal.write_text('{"type":"AGENT_CALL","response":"forged"}\n', encoding="utf-8")
    store.resume(item.id)

    out = _dispatch(store, "w")
    assert out.status == "failed"
    assert "journal changed while suspended" in out.result["message"]
