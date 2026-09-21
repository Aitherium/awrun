"""Every test gets its own audit log: lifecycle changes are recorded, and a test
run must never append to the operator's real trail."""

import pytest


@pytest.fixture(autouse=True)
def _scratch_audit_log(tmp_path, monkeypatch):
    monkeypatch.setenv("AWRUN_AUDIT_LOG", str(tmp_path / "awrun-audit.log"))
