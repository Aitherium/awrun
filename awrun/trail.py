"""Who changed a run, whether they were allowed to, and a record that says so.

Submitting a run that spends money or opens the perimeter is gated in `authz`.
This module covers the rest of a run's life -- suspend, resume, cancel, apply,
export, import -- with the same three questions in the same order:

* **who** -- a resolved identity session if one is presented, else the local
  OS user, labelled as such so the two can never be confused in the record;
* **may they** -- only when `AWRUN_LIFECYCLE_OPERATORS` names someone. Unset
  means a single-user box and nobody is asked; set means a resolved session in
  that list or a refusal, and a missing policy engine is a refusal, never a pass;
* **the record** -- written BEFORE the change, so a refused or crashed change
  still leaves a line. Best-effort by default; with `AWRUN_AUDIT_REQUIRED=1` a
  change that cannot be recorded does not happen.
"""

from __future__ import annotations

import getpass
import os
from typing import Any, Optional

from awrun import authz

LIFECYCLE_PERMISSION = "awrun:lifecycle"
_OPERATORS_ENV = "AWRUN_LIFECYCLE_OPERATORS"
_REQUIRED_ENV = "AWRUN_AUDIT_REQUIRED"


def _operators() -> list[str]:
    return [s.strip() for s in os.getenv(_OPERATORS_ENV, "").split(",") if s.strip()]


def actor() -> tuple[str, bool]:
    """(who, resolved). `resolved` is True only for a verified session."""
    subject = authz.resolve_session(os.getenv("AITHER_SESSION_BEARER", "").strip())
    if subject:
        return subject, True
    try:
        user = getpass.getuser()
    except (OSError, KeyError, ImportError):
        user = "unknown"
    return f"local:{user}", False


def authorize(who: str, resolved: bool) -> Optional[str]:
    operators = _operators()
    if not operators:
        return None
    if not resolved:
        return (f"{_OPERATORS_ENV} is set, so changing a run needs a resolved session "
                f"(AITHER_SESSION_BEARER); {who} is not one")
    try:
        from awbac import Policy
        policy = Policy().role("awrun-lifecycle", [LIFECYCLE_PERMISSION])
        for subject in operators:
            policy = policy.assign(subject, "awrun-lifecycle")
        decision = policy.check(who, LIFECYCLE_PERMISSION)
    except Exception as exc:  # noqa: BLE001 - no policy engine is a refusal, not a pass
        return f"could not evaluate the lifecycle policy ({type(exc).__name__}: {exc})"
    return None if decision else f"{who!r} may not change runs: {decision.reason}"


def guard(action: str, run_id: str = "", **fields: Any) -> Optional[str]:
    """Authorize and record one lifecycle change. None = go ahead; otherwise the
    refusal to show. Call it BEFORE the change."""
    who, resolved = actor()
    denial = authorize(who, resolved)
    record = authz.audit(f"run-{action}" + ("-denied" if denial else ""), run=run_id,
                         actor=who, resolved=resolved,
                         **({"reason": denial} if denial else {}), **fields)
    if denial:
        return denial
    if record is None and os.getenv(_REQUIRED_ENV, "").strip() == "1":
        return f"{action} refused: the audit record could not be written ({_REQUIRED_ENV}=1)"
    return None


def note(event: str, run_id: str, **fields: Any) -> None:
    """A fact that already happened (a runner parked or finished a run). Never
    blocks: there is nothing left to refuse."""
    authz.audit(f"run-{event}", run=run_id, **fields)


class scratch_log:  # noqa: N801 - used as `with trail.scratch_log():`
    """Point the audit log at a throwaway file for the duration. Self-tests run
    real lifecycle changes; the operator's real trail must not record them."""

    def __enter__(self):
        import tempfile
        self._dir = tempfile.TemporaryDirectory()
        self._saved = os.environ.get("AWRUN_AUDIT_LOG")
        os.environ["AWRUN_AUDIT_LOG"] = os.path.join(self._dir.name, "audit.log")
        return self

    def __exit__(self, *_exc):
        if self._saved is None:
            os.environ.pop("AWRUN_AUDIT_LOG", None)
        else:
            os.environ["AWRUN_AUDIT_LOG"] = self._saved
        self._dir.cleanup()


def self_test() -> int:
    import tempfile
    from pathlib import Path

    ok = True

    def check(label: str, cond: bool) -> None:
        nonlocal ok
        print(f"  {'ok' if cond else 'FAIL'} - {label}")
        if not cond:
            ok = False

    keys = ("AITHER_SESSION_BEARER", _OPERATORS_ENV, _REQUIRED_ENV,
            "AWRUN_AUDIT_LOG", "AWRUN_IAM_DIRECTORY")
    saved = {k: os.environ.pop(k, None) for k in keys}
    try:
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "audit.log"
            os.environ["AWRUN_AUDIT_LOG"] = str(log)
            os.environ["AWRUN_IAM_DIRECTORY"] = str(Path(td) / "iam.json")
            check("no operators named -> a local user is not asked", guard("suspend", "r-2222")
                  is None)
            os.environ[_OPERATORS_ENV] = "ops-dave"
            check("operators named -> an unresolved local user is REFUSED",
                  "resolved session" in (guard("suspend", "r-2222") or ""))
            judged_sessions = False
            try:
                from awiam import Directory, Sessions, Subject
            except ImportError:
                print("  -- identity brick absent: resolved-session cases not judged")
            else:
                judged_sessions = True
                directory = Directory(os.environ["AWRUN_IAM_DIRECTORY"])
                directory.put(Subject(id="ops-dave", display="Dave"))
                directory.put(Subject(id="mallory", display="M"))
                sessions = Sessions(directory)
                os.environ["AITHER_SESSION_BEARER"] = sessions.issue("mallory") or ""
                check("a resolved session NOT in the list is REFUSED",
                      "may not change runs" in (guard("resume", "r-2222") or ""))
                os.environ["AITHER_SESSION_BEARER"] = sessions.issue("ops-dave") or ""
                check("a resolved operator is allowed", guard("resume", "r-2222") is None)
            if log.exists():
                text = log.read_text(encoding="utf-8")
                check("the denial AND the allow are both on the record",
                      'run-suspend-denied"' in text and 'run-suspend"' in text
                      and (not judged_sessions
                           or ('run-resume-denied"' in text and 'run-resume"' in text)))
            os.environ.pop(_OPERATORS_ENV, None)
            os.environ.pop("AITHER_SESSION_BEARER", None)
            os.environ["AWRUN_AUDIT_LOG"] = str(Path(td) / "is-a-dir")
            (Path(td) / "is-a-dir").mkdir()
            os.environ[_REQUIRED_ENV] = "1"
            check("audit required + unwritable log -> the change is REFUSED",
                  "could not be written" in (guard("cancel", "r-2222") or ""))
            os.environ.pop(_REQUIRED_ENV)
            check("audit NOT required + unwritable log -> allowed",
                  guard("cancel", "r-2222") is None)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    print("TRAIL SELF-TEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(self_test())
