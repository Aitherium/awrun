"""awrun deploy / ps / rm: the record, the plan, and honesty about teardown."""

from __future__ import annotations

import json

import pytest
from awrun import cli, deploy, plugins


class FakeBackend:
    """Counts every call so a test can assert 'zero deployer calls'."""

    name = "FakeDeployer"

    def __init__(self, *, teardown: bool = True, ok: bool = True) -> None:
        self.calls: list[tuple] = []
        self._ok = ok
        if not teardown:
            self.teardown = None  # type: ignore[assignment]

    def deploy(self, agent, target, *, company=None):
        self.calls.append(("deploy", agent, target, company))
        if not self._ok:
            return {"ok": False, "error": "no_key_or_sdk", "guidance": "set a key"}
        return {"ok": True, "handle": f"agent_{agent}", "endpoint": "https://x.invalid/a",
                "detail": {"pack": agent}}

    def status(self, record):
        self.calls.append(("status", record["id"]))
        return {"status": "running", "live": True, "source": "fake-remote"}

    def teardown(self, record):  # noqa: F811 - may be shadowed per instance
        self.calls.append(("teardown", record["id"]))
        return {"ok": True, "remote_touched": True, "detail": "archived"}


@pytest.fixture(autouse=True)
def _scratch(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_AWRUN_DIR", str(tmp_path / "awrun"))
    monkeypatch.setenv("AWRUN_DEPLOYER_PLUGIN", str(tmp_path / "absent.py"))
    plugins.clear()
    yield
    plugins.clear()


def _register(backend):
    plugins.register(plugins.AGENT_DEPLOYER, lambda: backend)
    return backend


def test_round_trip_deploy_ps_rm():
    be = _register(FakeBackend())
    code, out = deploy.deploy("atlas", "managed", company="acme")
    assert code == 0
    dep = out["deployment"]
    assert dep["status"] == "deployed" and dep["handle"] == "agent_atlas"
    assert dep["backend"] == "FakeDeployer" and dep["id"].startswith("d-")
    assert out["plan"]["agent"] == "atlas"

    rows = deploy.ps()
    assert [r["id"] for r in rows] == [dep["id"]]
    assert rows[0]["current_status"] == "running" and rows[0]["live"] is True
    assert rows[0]["status_source"] == "fake-remote"

    code, out = deploy.rm(dep["id"], yes=True)
    assert code == 0 and "warning" not in out
    assert out["deployment"]["status"] == "removed"
    assert out["deployment"]["remote_touched"] is True
    assert ("teardown", dep["id"]) in be.calls
    assert deploy.ps() == []
    assert [r["status"] for r in deploy.ps(include_removed=True)] == ["removed"]


def test_rm_without_teardown_says_remote_not_touched():
    _register(FakeBackend(teardown=False))
    _, out = deploy.deploy("atlas", "managed")
    dep_id = out["deployment"]["id"]
    code, out = deploy.rm(dep_id, yes=True)
    assert code == 0
    assert "REMOTE NOT TOUCHED" in out["warning"]
    assert "agent_atlas" in out["warning"]
    assert out["deployment"]["remote_touched"] is False
    assert out["deployment"]["status"] == "removed"


def test_rm_unsupported_teardown_is_same_as_none():
    be = FakeBackend()
    be.teardown = lambda record: {"supported": False}  # type: ignore[assignment]
    _register(be)
    _, out = deploy.deploy("atlas", "managed")
    code, out = deploy.rm(out["deployment"]["id"], yes=True)
    assert code == 0 and "REMOTE NOT TOUCHED" in out["warning"]


def test_rm_requires_yes_and_changes_nothing():
    be = _register(FakeBackend())
    _, out = deploy.deploy("atlas", "managed")
    dep_id = out["deployment"]["id"]
    code, out = deploy.rm(dep_id)
    assert code == 1 and "--yes" in out["error"]
    assert not [c for c in be.calls if c[0] == "teardown"]
    assert deploy.DeploymentStore().get(dep_id).status == "deployed"


def test_failed_teardown_leaves_record():
    be = FakeBackend()
    be.teardown = lambda record: {"ok": False, "error": "403"}  # type: ignore[assignment]
    _register(be)
    _, out = deploy.deploy("atlas", "managed")
    dep_id = out["deployment"]["id"]
    code, out = deploy.rm(dep_id, yes=True)
    assert code == 1 and "403" in out["error"]
    assert deploy.DeploymentStore().get(dep_id).status == "deployed"


def test_dry_run_makes_zero_deployer_calls():
    factory_calls = []
    be = FakeBackend()

    def factory():
        factory_calls.append(1)
        return be

    plugins.register(plugins.AGENT_DEPLOYER, factory)
    code, out = deploy.deploy("atlas", "managed", dry_run=True)
    assert code == 0 and out["dry_run"] is True
    assert out["plan"]["backend_registered"] is True
    assert be.calls == [] and factory_calls == []
    assert deploy.DeploymentStore().list() == []


def test_cli_dry_run_never_loads_or_calls(capsys, monkeypatch):
    loaded = []
    monkeypatch.setattr(cli, "_register_deployer", lambda: loaded.append(1))
    assert cli.main(["deploy", "atlas", "--dry-run"]) == 0
    assert loaded == []
    assert "no deployer was called" in capsys.readouterr().out


def test_failed_deploy_is_recorded_with_guidance():
    _register(FakeBackend(ok=False))
    code, out = deploy.deploy("atlas", "managed")
    assert code == 1
    dep = out["deployment"]
    assert dep["status"] == "failed" and dep["error"] == "no_key_or_sdk"
    assert dep["detail"]["guidance"] == "set a key"
    rows = deploy.ps()
    assert rows[0]["status_source"] == deploy.LAST_KNOWN and rows[0]["live"] is False


def test_no_backend_exits_2_and_records_nothing():
    code, out = deploy.deploy("atlas", "managed")
    assert code == 2 and "no deployer backend" in out["error"]
    assert deploy.DeploymentStore().list() == []


def test_ps_without_backend_is_labelled_last_known():
    _register(FakeBackend())
    deploy.deploy("atlas", "managed")
    plugins.clear()
    rows = deploy.ps()
    assert rows[0]["status_source"] == deploy.LAST_KNOWN and rows[0]["live"] is False


def test_bad_target_and_bad_id():
    assert deploy.deploy("atlas", "orbit")[0] == 2
    assert deploy.rm("r-notadeploy", yes=True)[0] == 2


def test_cli_round_trip_json(capsys):
    _register(FakeBackend(teardown=False))
    assert cli.main(["deploy", "atlas", "--target", "local", "--json"]) == 0
    dep = json.loads(capsys.readouterr().out)["deployment"]
    assert cli.main(["ps", "--json"]) == 0
    assert [r["id"] for r in json.loads(capsys.readouterr().out)] == [dep["id"]]
    assert cli.main(["rm", dep["id"]]) == 1
    capsys.readouterr()
    assert cli.main(["rm", dep["id"], "--yes"]) == 0
    assert "REMOTE NOT TOUCHED" in capsys.readouterr().out
