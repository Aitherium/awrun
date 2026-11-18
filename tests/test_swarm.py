"""The swarm path: a task list goes out as one `ci` run, and its report comes back
into the queue. GitHub is replaced by a scripted `gh`; nothing here touches a network."""

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from awrun import cli, dispatcher, swarm
from awrun.store import RunStore

PKG = Path(__file__).resolve().parent.parent


def _fake_gh(report, *, title_has_id=True, conclusion="success", calls=None):
    """A `gh` that dispatches, lists one run, reports it complete and 'downloads'."""
    def gh(argv, timeout=120):
        if calls is not None:
            calls.append(argv)
        if argv[1:3] == ["workflow", "run"]:
            gh.run_id = next((a.split("=", 1)[1] for a in argv
                              if a.startswith("awrun_id=")), "")
            return 0, ""
        if argv[1:3] == ["run", "list"]:
            title = f"bonsai-swarm {gh.run_id} (4B)" if title_has_id else "bonsai-swarm (4B)"
            return 0, json.dumps([
                {"databaseId": 7, "displayTitle": "bonsai-swarm r-someoneelse (4B)", "url": "u7"},
                {"databaseId": 9, "displayTitle": title, "url": "https://example.test/runs/9"}])
        if argv[1:3] == ["run", "view"]:
            return 0, json.dumps({"status": "completed", "conclusion": conclusion})
        if argv[1:3] == ["run", "download"]:
            if report is None:
                return 1, "no artifact found"
            dest = Path(argv[argv.index("-D") + 1])
            (dest / "swarm-report.json").write_text(json.dumps(report), encoding="utf-8")
            return 0, ""
        return 1, f"unexpected gh call: {argv}"
    gh.run_id = ""
    return gh


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_AWRUN_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(dispatcher, "collect_dir", lambda i: tmp_path / "artifacts" / i)
    monkeypatch.setattr(dispatcher, "_relay_client", lambda: None)
    return RunStore(tmp_path / "runs")


def _tasks(tmp_path, tasks):
    p = tmp_path / "tasks.json"
    p.write_text(json.dumps({"tasks": tasks}), encoding="utf-8")
    return str(p)


REPORT = {"tasks": 1, "missing": [], "verdicts": {"PASS": 1}, "graded": 1, "pass_rate": 1.0,
          "tok_per_s": {"min": 9.5, "max": 9.5, "mean": 9.5},
          "results": [{"id": "t1", "repo": "Aitherium/awrun", "verdict": "PASS", "steps": 3,
                       "tok_per_s": 9.5, "wall_s": 40.0, "answer": "0.2.8"}]}


def test_a_ci_run_without_collect_is_still_fire_and_forget(store, monkeypatch):
    calls = []
    monkeypatch.setattr(dispatcher, "_gh", _fake_gh(REPORT, calls=calls))
    item = store.submit("ci", {"workflow": "x.yml", "repo": "o/r"})
    done = dispatcher.dispatch_once(store, worker_id="t")
    assert done.status == "done"
    assert len(calls) == 1, "a plain ci run must not wait on, or look for, the run"
    assert not any("awrun_id=" in a for a in calls[0])
    assert item.id == done.id


def test_collect_finds_its_own_run_and_brings_the_artifact_home(store, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(dispatcher, "_gh", _fake_gh(REPORT, calls=calls))
    item = store.submit("ci", {"workflow": "bonsai-swarm.yml", "repo": "Aitherium/awrun",
                               "ref": "main", "inputs": {"model_size": "4B"},
                               "collect": {"artifact": "swarm-report", "poll_s": 0}})
    done = dispatcher.dispatch_once(store, worker_id="t")
    assert done.status == "done"
    info = json.loads(done.result["message"])
    # Run 7 is somebody else's and is listed FIRST: the id in the title picks ours.
    assert info["run_id"] == "9"
    assert info["files"] == ["swarm-report.json"]
    assert f"awrun_id={item.id}" in calls[0]
    report, why = cli.swarm_report(done)
    assert why == "" and report["pass_rate"] == 1.0
    assert report["run_url"] == "https://example.test/runs/9"


def test_a_run_that_never_shows_the_id_fails_and_says_what_the_workflow_lacks(
        store, monkeypatch):
    monkeypatch.setattr(dispatcher, "_gh", _fake_gh(REPORT, title_has_id=False))
    clock = iter(range(0, 100000, 60))
    item = store.submit("ci", {"workflow": "w.yml", "repo": "o/r",
                               "collect": {"artifact": "a", "timeout_s": 600}})
    code, msg = dispatcher._collect_ci(item, gh=dispatcher._gh, sleep_fn=lambda s: None,
                                       now_fn=lambda: next(clock))
    assert code == 1 and "run-name" in msg


def test_a_finished_run_with_no_artifact_is_failed_not_done(store, monkeypatch):
    monkeypatch.setattr(dispatcher, "_gh", _fake_gh(None))
    store.submit("ci", {"workflow": "w.yml", "repo": "o/r", "collect": {"artifact": "a"}})
    done = dispatcher.dispatch_once(store, worker_id="t")
    assert done.status == "failed"
    assert "could not be downloaded" in json.loads(done.result["message"])["error"]
    report, why = cli.swarm_report(done)
    assert report is None and "could not be downloaded" in why


def test_swarm_submit_run_returns_the_report_and_result_reads_it_again(
        store, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(dispatcher, "_gh", _fake_gh(REPORT))
    monkeypatch.setattr(cli, "get_store", lambda: store)
    monkeypatch.setattr(cli, "_register_capacity_provider", lambda: None)
    path = _tasks(tmp_path, [{"id": "t1", "goal": "version?", "repo": "Aitherium/awrun"}])
    assert cli.main(["swarm", "submit", path, "--run", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "done" and out["results"][0]["answer"] == "0.2.8"
    assert cli.main(["swarm", "result", out["id"]]) == 0
    assert "| t1 | Aitherium/awrun | PASS |" in capsys.readouterr().out


def test_swarm_submit_refuses_a_repo_outside_the_workflow_owner(
        store, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "get_store", lambda: store)
    monkeypatch.setattr(cli, "_register_capacity_provider", lambda: None)
    path = _tasks(tmp_path, [{"id": "t1", "goal": "g", "repo": "someone/else"}])
    assert cli.main(["swarm", "submit", path]) == 2
    assert "outside Aitherium" in capsys.readouterr().err
    assert store.list() == [], "a refused swarm must leave nothing queued"


def test_the_loop_is_runnable_as_a_bare_file_with_nothing_installed():
    """A runner executes awrun/swarm.py from a sparse checkout of that ONE file."""
    r = subprocess.run([sys.executable, "-I", str(PKG / "awrun" / "swarm.py"), "--self-test"],
                       capture_output=True, text=True, encoding="utf-8", cwd=str(PKG.parent))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "self-test: PASS" in r.stdout


def test_combine_names_a_task_that_never_reported(tmp_path):
    (tmp_path / "f.txt").write_text("x", encoding="utf-8")
    script = iter(['{"tool": "read_file", "args": {"path": "f.txt"}}', '{"final": "x"}'])
    res = swarm.solve({"id": "a", "goal": "g"}, lambda m: next(script), tmp_path,
                      log=lambda s: None)
    assert res["verdict"] == "ANSWERED"
    part = tmp_path / "a.json"
    part.write_text(json.dumps(res), encoding="utf-8")
    rep = swarm.combine([str(part)], expect=["a", "b"])
    assert rep["missing"] == ["b"] and rep["tasks"] == 1


def test_the_workflow_puts_the_callers_id_in_the_run_name():
    """collect finds its run by that id; a workflow without it can never be collected."""
    text = (PKG / ".github" / "workflows" / "bonsai-swarm.yml").read_text(encoding="utf-8")
    run_name = next(ln for ln in text.splitlines() if ln.startswith("run-name:"))
    assert "inputs." + dispatcher.COLLECT_ID_INPUT in run_name
    assert "secrets." not in text, "the swarm jobs hold no secret"


def test_the_workflow_cannot_be_started_by_a_stranger_and_pins_what_it_runs():
    text = (PKG / ".github" / "workflows" / "bonsai-swarm.yml").read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    top = [ln.rstrip(":") for ln in lines if ln.startswith("  ") and not ln.startswith("   ")
           and lines.index(ln) < lines.index("permissions:")]
    assert sorted(top) == ["  workflow_call", "  workflow_dispatch"], top
    for banned in ("pull_request", "issue_comment", "schedule:", "secrets."):
        assert not any(banned in ln for ln in lines), f"{banned} appears in the workflow"
    assert sum(ln.strip().startswith("permissions:") for ln in lines) == 1
    assert lines[lines.index("permissions:") + 1].strip() == "contents: read"
    uses = [ln.split("uses:", 1)[1].split("#")[0].strip() for ln in lines if "uses:" in ln]
    assert uses and all(re.search(r"@[0-9a-f]{40}$", u) for u in uses), uses
    minutes = [int(ln.split(":")[1]) for ln in lines if "timeout-minutes:" in ln]
    assert len(minutes) == 3 and max(minutes) <= 45
    assert "resolve/main/" not in text, "the model must come from a pinned revision"


def test_no_owner_named_means_no_repository_is_allowed():
    with pytest.raises(ValueError, match="no owner was named"):
        swarm.validate_tasks([{"id": "a", "goal": "g", "repo": "Aitherium/awrun"}], "")
    with pytest.raises(ValueError, match="at most"):
        swarm.validate_tasks([{"id": f"t{i}", "goal": "g"}
                              for i in range(swarm.MAX_TASKS + 1)], "")


def test_an_answer_nobody_looked_up_is_never_a_pass(tmp_path):
    """Run 43746857617: a 2B model asked for an invented path, got an error, then answered
    a version-shaped guess, and a version-shaped check graded it PASS."""
    (tmp_path / "pyproject.toml").write_text('version = "1.14.1"', encoding="utf-8")
    script = iter(['{"tool": "read_file", "args": {"path": "/path/to/pyproject.toml"}}',
                   '{"final": "1.14.1"}'])
    seen = []

    def chat(messages):
        seen.append(messages[-1]["content"])
        return next(script)

    res = swarm.solve({"id": "v", "goal": "version?", "check": "1.14.1"}, chat, tmp_path,
                      log=lambda s: None)
    assert res["verdict"] == "UNGROUNDED" and res["tool_calls_ok"] == 0
    assert 'use "pyproject.toml"' in seen[-1]
    assert "UNGROUNDED" in swarm.FAILURES


def test_a_check_that_accepts_a_placeholder_is_refused():
    bs = chr(92)
    loose = bs + "b" + bs + "d+" + bs + "." + bs + "d+" + bs + "." + bs + "d+" + bs + "b"
    with pytest.raises(ValueError, match="too loose"):
        swarm.validate_tasks([{"id": "a", "goal": "g", "check": loose}], "")
    pinned = bs + "b1" + bs + ".14" + bs + ".1" + bs + "b"
    assert swarm.validate_tasks([{"id": "a", "goal": "g", "check": pinned}], "")


def test_a_task_list_carrying_a_secret_or_an_internal_name_is_refused_not_redacted(
        store, monkeypatch, tmp_path, capsys):
    calls = []
    monkeypatch.setattr(dispatcher, "_gh", _fake_gh(REPORT, calls=calls))
    monkeypatch.setattr(cli, "get_store", lambda: store)
    monkeypatch.setattr(cli, "_register_capacity_provider", lambda: None)
    extra = tmp_path / "patterns.txt"
    extra.write_text("# host names\nticket-id  TICKET-[0-9]+\n", encoding="utf-8")
    monkeypatch.setenv("AWRUN_SWARM_BOUNDARY", str(extra))
    token = "gh" + "p_" + "b" * 36
    for goal, why in (("clone with " + token, "github-token"),
                      ("explain TICKET-4411", "ticket-id")):
        path = _tasks(tmp_path, [{"id": "t1", "goal": goal, "repo": "Aitherium/awrun"}])
        assert cli.main(["swarm", "submit", path, "--run"]) == 2
        err = capsys.readouterr().err
        assert why in err and token not in err, "the finding must never echo the secret"
    assert store.list() == [] and calls == [], "refused means nothing queued, nothing sent"
    monkeypatch.setenv("AWRUN_SWARM_BOUNDARY", str(tmp_path / "missing.txt"))
    path = _tasks(tmp_path, [{"id": "t1", "goal": "fine", "repo": "Aitherium/awrun"}])
    assert cli.main(["swarm", "submit", path]) == 2, "a named but missing file must refuse"
