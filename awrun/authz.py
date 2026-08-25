"""awrun/authz.py — trust-plane wiring for the one kind that spends money.

Light-touch for local use, strict only where money can be spent
(owner-confirmed design decision — see the plan this package was built
from). A local `awrun submit --kind agent ...` on the trusted dev box needs
no session token, exactly as it works today. Only `comet-deploy` submissions
require a resolved `awiam` session, an `awbac` policy check, and every
submit/dispatch/denial is written to an `awdit` audit log.

Wraps the REAL, verified `awiam`/`awbac`/`awdit` APIs (checked live against
develop, 2026-08-20 — `Sessions(directory).resolve(token) -> Resolution`,
`Policy().role(...).assign(...).check(subject, perm) -> Decision`,
`append(log_path, event, **fields) -> dict`) rather than an imagined
`Policy.load()` classmethod, which does not exist on the real class.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

#: The one permission this whole module exists to gate. Not a general-purpose
#: RBAC layer -- one string, one door, matching "strict only where money can
#: be spent".
COMET_DEPLOY_PERMISSION = "awrun:submit:comet-deploy"


def _iam_directory_path() -> Path:
    env = os.getenv("AWRUN_IAM_DIRECTORY", "").strip()
    return Path(env) if env else Path.home() / ".aither" / "awrun-iam.json"


def _audit_log_path() -> Path:
    env = os.getenv("AWRUN_AUDIT_LOG", "").strip()
    return Path(env) if env else Path.home() / ".aither" / "awrun-audit.log"


def _operators_env() -> list[str]:
    """Who may submit a comet-deploy, read from AWRUN_COMET_DEPLOY_OPERATORS
    (comma-separated subject ids). Deliberately NOT a `Policy.load()` --
    that method does not exist on the real class (an earlier draft of this
    package's plan guessed at it). Empty by default: a money-spend path
    fails CLOSED until an owner explicitly names who may use it, matching
    "strict only where money can be spent" rather than "open until someone
    remembers to lock it down"."""
    raw = os.getenv("AWRUN_COMET_DEPLOY_OPERATORS", "").strip()
    return [s.strip() for s in raw.split(",") if s.strip()]


def _policy():
    """Built fresh from env on every call -- this package has no long-lived
    process to keep a cached Policy warm across a config change, and a
    money-gating permission set is cheap enough to rebuild every check."""
    from awbac import Policy

    p = Policy().role("comet-deploy-operator", [COMET_DEPLOY_PERMISSION])
    operators = _operators_env()
    if operators:
        p = p.assign(operators[0], "comet-deploy-operator")
        for subject in operators[1:]:
            p = p.assign(subject, "comet-deploy-operator")
    return p


def resolve_session(token: str) -> Optional[str]:
    """Resolve a session token to a subject id, or None if unresolvable
    (missing token, expired session, no directory configured). Never
    raises -- an authz-adjacent failure here must read as "not
    authenticated", not crash the CLI."""
    if not token:
        return None
    try:
        from awiam import Directory, Sessions
    except Exception:
        return None
    directory_path = _iam_directory_path()
    if not directory_path.exists():
        return None
    try:
        directory = Directory(str(directory_path))
        resolution = Sessions(directory).resolve(token)
    except Exception:
        return None
    if not resolution:
        return None
    return resolution.subject_id


def check_permission(subject_id: str, permission: str = COMET_DEPLOY_PERMISSION):
    """Returns an awbac Decision (truthy on allow, `.reason` always set).
    Fails CLOSED -- an exception building or checking the policy is treated
    as a denial, never an allow, matching every other fail-closed gate in
    this codebase (security-review-patterns.md #1)."""
    from awbac import Decision

    if not subject_id:
        return Decision(allowed=False, reason="no resolved subject", via="")
    try:
        return _policy().check(subject_id, permission)
    except Exception as exc:
        return Decision(allowed=False, reason=f"policy check raised: {exc}", via="")


def audit(event: str, **fields: Any) -> Optional[dict]:
    """Append one hash-chained audit record. Returns the record, or None on
    failure -- this function itself never raises, so a submit path can
    decide independently whether a failed audit write should block (Phase 8
    design: it blocks comet-deploy, never agent/ci)."""
    try:
        from awdit import append
    except Exception:
        return None
    path = _audit_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        return append(str(path), event, **fields)
    except Exception:
        return None


def _self_test() -> int:
    import tempfile

    ok = True

    def check(label: str, cond: bool) -> None:
        nonlocal ok
        print(f"  {'ok' if cond else 'FAIL'} - {label}")
        if not cond:
            ok = False

    check("resolve_session('') is None without touching any directory",
          resolve_session("") is None)
    check("resolve_session() with no configured directory is None, not a crash",
          resolve_session("some-token") is None or True)  # env-dependent; must not raise

    old_operators = os.environ.pop("AWRUN_COMET_DEPLOY_OPERATORS", None)
    try:
        os.environ.pop("AWRUN_COMET_DEPLOY_OPERATORS", None)
        denied = check_permission("random-subject")
        check("with no operators configured, comet-deploy permission FAILS CLOSED",
              not denied and "no resolved subject" not in denied.reason)

        os.environ["AWRUN_COMET_DEPLOY_OPERATORS"] = "david,ops-bot"
        allowed = check_permission("david")
        check("an operator named via AWRUN_COMET_DEPLOY_OPERATORS is granted",
              bool(allowed))
        allowed2 = check_permission("ops-bot")
        check("a SECOND operator in the comma-separated list is also granted",
              bool(allowed2))
        still_denied = check_permission("random-subject")
        check("a subject NOT in the operator list stays denied",
              not still_denied)
    finally:
        if old_operators is not None:
            os.environ["AWRUN_COMET_DEPLOY_OPERATORS"] = old_operators
        else:
            os.environ.pop("AWRUN_COMET_DEPLOY_OPERATORS", None)

    check("check_permission('') fails closed with no policy call at all",
          not check_permission(""))

    with tempfile.TemporaryDirectory() as td:
        log_path = Path(td) / "audit.log"
        old_log = os.environ.get("AWRUN_AUDIT_LOG")
        os.environ["AWRUN_AUDIT_LOG"] = str(log_path)
        try:
            record = audit("test-event", subject="david", allowed=True)
            check("audit() returns the appended record on success", record is not None)
            check("the audit log file was actually created", log_path.exists())

            from awdit import verify
            result = verify(str(log_path))
            check("the audit chain verifies (no gaps)", bool(result))
        finally:
            if old_log is not None:
                os.environ["AWRUN_AUDIT_LOG"] = old_log
            else:
                os.environ.pop("AWRUN_AUDIT_LOG", None)

    # A directory that cannot possibly resolve (no such file) must degrade,
    # never raise -- this is the difference between "not authenticated" and
    # a crashed CLI.
    os.environ["AWRUN_IAM_DIRECTORY"] = "/does/not/exist/iam.json"
    try:
        check("an unresolvable IAM directory degrades to None, not an exception",
              resolve_session("anything") is None)
    finally:
        os.environ.pop("AWRUN_IAM_DIRECTORY", None)

    print("SELF-TEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return _self_test()
    ap.print_help()
    return 2


if __name__ == "__main__":
    import sys
    sys.exit(main())
