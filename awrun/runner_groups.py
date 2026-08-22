"""Reserve CI capacity for a workflow, because labels cannot.

WHY THIS EXISTS
---------------
Labels MATCH, they do not EXCLUDE. A job asking for `[self-hosted, Linux]` is
served by any runner carrying those two, and every self-hosted runner must carry
them -- so a "reserved" label reserves nothing.

Measured 2026-08-22, and it cost a production site ten hours. A deploy workflow
was evicted from its queue 11 times out of 30 runs: a concurrency group permits
exactly ONE queued run, the branch was pushed every 2-5 minutes, and with every
shared runner saturated the queued deploy was discarded by the next push before
it ever started. Nothing failed, so nothing paged -- the site simply served a
stale bundle.

Two label-based attempts failed in ways worth recording, because both look
correct:

  1. ADD a dedicated label to a runner. It stays in the shared pool, so ordinary
     CI takes it anyway -- measured, within seconds.
  2. REMOVE the shared label from that runner. Better, and still not exclusive:
     a STALE BRANCH copy of another workflow requesting the generic pair took it.
     Old branches carry old workflow definitions, and no label edit reaches them.

The mechanism that DOES exclude is an org runner group with
`restricted_to_workflows`. A runner in such a group is schedulable only by the
named workflows, on any branch, permanently. That is the difference between
matching and access, and it is the only version of "reserved" that holds.

STANDALONE ON PURPOSE
---------------------
stdlib only, and no monorepo import: this package ships to PyPI, where `lib.*`
does not exist. The token comes from GH_TOKEN / GITHUB_TOKEN, never an argv --
a token in a command line is in the process table for every other user on the
box and in the shell history forever.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Optional

API = "https://api.github.com"


class RunnerGroupError(RuntimeError):
    """Something we could not do, stated plainly enough to act on."""


def _token() -> str:
    for var in ("GH_TOKEN", "GITHUB_TOKEN", "AWRUN_GH_TOKEN"):
        v = os.environ.get(var, "").strip()
        if v:
            return v
    raise RunnerGroupError(
        "no GitHub token: set GH_TOKEN (or GITHUB_TOKEN). It is read from the "
        "environment and never accepted as an argument, because an argv token "
        "is visible in the process table and stays in shell history.")


def _api(path: str, method: str = "GET", body: Optional[dict] = None,
         timeout: float = 20.0) -> Any:
    req = urllib.request.Request(
        f"{API}{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {_token()}",
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28",
                 "Content-Type": "application/json",
                 "User-Agent": "awrun-runner-groups"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode()[:300]
        except Exception:  # noqa: BLE001
            pass
        # 403 here is nearly always "this token is not an org admin", and saying
        # so is the difference between a fix and an afternoon.
        hint = (" -- the token needs org admin (admin:org) to manage runner "
                "groups" if e.code in (403, 404) else "")
        raise RunnerGroupError(f"{method} {path} -> HTTP {e.code}{hint}: {detail}") from e
    except Exception as e:  # noqa: BLE001
        raise RunnerGroupError(f"{method} {path} -> {type(e).__name__}: {e}") from e


# ── read ────────────────────────────────────────────────────────────────────

def list_groups(org: str) -> list[dict]:
    return _api(f"/orgs/{org}/actions/runner-groups").get("runner_groups", [])


def find_group(org: str, name: str) -> Optional[dict]:
    for g in list_groups(org):
        if g.get("name") == name:
            return g
    return None


def group_runners(org: str, group_id: int) -> list[dict]:
    return _api(f"/orgs/{org}/actions/runner-groups/{group_id}/runners").get("runners", [])


def org_runners(org: str) -> list[dict]:
    return _api(f"/orgs/{org}/actions/runners?per_page=100").get("runners", [])


def describe(org: str, name: str) -> dict:
    """Everything needed to judge whether a reservation is REAL."""
    g = find_group(org, name)
    if not g:
        raise RunnerGroupError(f"no runner group named {name!r} in {org}")
    runners = group_runners(org, g["id"])
    return {
        "id": g["id"],
        "name": g["name"],
        # The load-bearing field. A group that is not restricted is not a
        # reservation -- every workflow in the org can schedule onto it.
        "restricted_to_workflows": bool(g.get("restricted_to_workflows")),
        "selected_workflows": g.get("selected_workflows", []),
        "visibility": g.get("visibility"),
        "runners": [{"id": r["id"], "name": r["name"], "status": r.get("status"),
                     "busy": r.get("busy"),
                     "labels": [x["name"] for x in r.get("labels", [])]}
                    for r in runners],
    }


# ── write ───────────────────────────────────────────────────────────────────

def reserve(org: str, name: str, workflow: str, runner: str,
            label: str = "") -> dict:
    """Reserve one runner for one workflow. Idempotent.

    `workflow` is the full GitHub form, ref included:
        Org/Repo/.github/workflows/x.yml@refs/heads/develop
    A path without `@ref` is rejected by the API, and the error does not say so.

    A label is ALSO applied when given, and both halves are needed for different
    reasons: the GROUP decides who MAY use the runner, the LABEL is how the
    workflow asks for it. Neither alone reserves anything.
    """
    if "@" not in workflow:
        raise RunnerGroupError(
            f"workflow must name a ref: {workflow!r} -- GitHub wants "
            "'Org/Repo/.github/workflows/file.yml@refs/heads/BRANCH', and "
            "rejects a bare path with an error that does not mention the ref")

    g = find_group(org, name)
    if not g:
        g = _api(f"/orgs/{org}/actions/runner-groups", "POST",
                 {"name": name, "visibility": "all"})

    wanted = sorted({*(g.get("selected_workflows") or []), workflow})
    _api(f"/orgs/{org}/actions/runner-groups/{g['id']}", "PATCH",
         {"name": name, "restricted_to_workflows": True,
          "selected_workflows": wanted})

    match = [r for r in org_runners(org) if r.get("name") == runner]
    if not match:
        raise RunnerGroupError(
            f"no ORG runner named {runner!r}. Repo-level runners cannot join an "
            "org runner group -- register it at the org, or pick an org runner.")
    rid = match[0]["id"]
    _api(f"/orgs/{org}/actions/runner-groups/{g['id']}/runners/{rid}", "PUT")

    if label:
        have = [x["name"] for x in match[0].get("labels", [])]
        if label not in have:
            _api(f"/orgs/{org}/actions/runners/{rid}/labels", "POST",
                 {"labels": [label]})

    return describe(org, name)


def release(org: str, name: str, runner: str) -> dict:
    """Return a runner to general availability. The group is left in place."""
    g = find_group(org, name)
    if not g:
        raise RunnerGroupError(f"no runner group named {name!r} in {org}")
    match = [r for r in group_runners(org, g["id"]) if r.get("name") == runner]
    if not match:
        raise RunnerGroupError(f"{runner!r} is not in group {name!r}")
    _api(f"/orgs/{org}/actions/runner-groups/{g['id']}/runners/{match[0]['id']}",
         "DELETE")
    return describe(org, name)


def audit(org: str) -> list[str]:
    """Reservations that are not reservations. Empty list = every group holds.

    A group with runners but no workflow restriction is the failure this module
    exists for, wearing the right name: it LOOKS reserved in every listing and
    admits the whole org.
    """
    out: list[str] = []
    for g in list_groups(org):
        if g.get("default"):
            continue
        n = len(group_runners(org, g["id"]))
        if n and not g.get("restricted_to_workflows"):
            out.append(f"{g['name']}: {n} runner(s) and NO workflow restriction "
                       f"-- any workflow in {org} can schedule onto them")
        if g.get("restricted_to_workflows") and not g.get("selected_workflows"):
            out.append(f"{g['name']}: restricted but names NO workflow -- "
                       "nothing can use these runners at all")
    return out


# ── criticality: which workflows may never queue behind ordinary CI ─────────
#
# Reserved capacity is FINITE and reserving is not free -- every runner moved
# into a restricted group leaves the shared pool, so the CI everything else
# depends on gets slower. That makes this an allocation decision, and an
# allocation decision made implicitly is made badly: on 2026-08-22 the thing
# that lost was a production deploy, silently, for ten hours.
#
# Criticality is expressed on awrun's OWN priority scale (higher = more urgent)
# so a workflow and a queued run are ranked on one axis rather than two
# vocabularies that drift.

CRITICAL = 90    #: user-visible if it stops. Never queues behind CI.
IMPORTANT = 50   #: wanted promptly; may wait behind a CRITICAL one.
ROUTINE = 0      #: the shared pool is correct for these.

#: The threshold at which a workflow earns reserved capacity. Deliberately just
#: below CRITICAL: the point of the scale is that "critical" is a claim someone
#: makes on the record, not a mood.
RESERVE_AT = CRITICAL


def plan(policy: dict[str, int], runners: list[str],
         reserve_at: int = RESERVE_AT) -> dict[str, Any]:
    """Who gets a reserved runner, given finite capacity. PURE -- no API calls.

    Returns {"assign": [(workflow, runner)], "unfunded": [workflow],
             "spare": [runner]}.

    `unfunded` is the honest part and the reason this returns rather than acts:
    when there are more critical workflows than runners, SOMETHING does not get
    reserved, and a planner that silently reserved the first N would hide
    exactly the decision a human needs to see. Highest priority wins; ties break
    on name so the plan is reproducible rather than dict-ordered.
    """
    wanted = sorted(((w, p) for w, p in policy.items() if p >= reserve_at),
                    key=lambda kv: (-kv[1], kv[0]))
    pool = list(runners)
    assign: list[tuple[str, str]] = []
    unfunded: list[str] = []
    for wf, _prio in wanted:
        if pool:
            assign.append((wf, pool.pop(0)))
        else:
            unfunded.append(wf)
    return {"assign": assign, "unfunded": unfunded, "spare": pool}


def apply_policy(org: str, policy: dict[str, int], runners: list[str],
                 group: str = "deploy", dry_run: bool = True) -> dict[str, Any]:
    """Reconcile reservations against the declared criticality.

    Defaults to DRY RUN: this moves runners out of the shared pool, which slows
    everything else, so the caller says so explicitly.
    """
    p = plan(policy, runners)
    p["applied"] = []
    if dry_run:
        p["dry_run"] = True
        return p
    for wf, runner in p["assign"]:
        p["applied"].append(describe_safe(org, group, wf, runner))
    p["dry_run"] = False
    return p


def describe_safe(org: str, group: str, workflow: str, runner: str) -> dict:
    """reserve(), but a failure on one workflow does not abandon the rest."""
    try:
        reserve(org, group, workflow, runner, label=group)
        return {"workflow": workflow, "runner": runner, "ok": True}
    except RunnerGroupError as exc:
        return {"workflow": workflow, "runner": runner, "ok": False,
                "error": str(exc)}


def self_test() -> int:
    """Prove the guards fire without touching GitHub."""
    fails = []
    try:
        reserve("o", "g", "no-ref-here.yml", "r")
        fails.append("accepted a workflow with no @ref")
    except RunnerGroupError as e:
        if "ref" not in str(e):
            fails.append(f"wrong error for a missing ref: {e}")

    saved = {k: os.environ.pop(k, None)
             for k in ("GH_TOKEN", "GITHUB_TOKEN", "AWRUN_GH_TOKEN")}
    try:
        _token()
        fails.append("returned a token when none is set")
    except RunnerGroupError as e:
        if "GH_TOKEN" not in str(e):
            fails.append(f"unhelpful missing-token error: {e}")
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v

    # planner: highest priority is funded first, and scarcity is REPORTED
    pol = {"a.yml@refs/heads/main": CRITICAL,
           "b.yml@refs/heads/main": CRITICAL,
           "c.yml@refs/heads/main": ROUTINE}
    got = plan(pol, ["r1"])
    if [w for w, _ in got["assign"]] != ["a.yml@refs/heads/main"]:
        fails.append(f"planner did not fund the first critical workflow: {got}")
    if got["unfunded"] != ["b.yml@refs/heads/main"]:
        fails.append(f"planner hid an unfunded critical workflow: {got}")
    if any(w.startswith("c.yml") for w, _ in got["assign"]):
        fails.append("planner reserved capacity for a ROUTINE workflow")

    # ...and it must not invent scarcity when there is enough
    got2 = plan(pol, ["r1", "r2", "r3"])
    if got2["unfunded"] or len(got2["assign"]) != 2 or got2["spare"] != ["r3"]:
        fails.append(f"planner mis-allocated a sufficient pool: {got2}")

    # apply_policy must not touch anything unless told
    ap = apply_policy("o", pol, ["r1"], dry_run=True)
    if not ap.get("dry_run") or ap.get("applied"):
        fails.append("apply_policy acted during a dry run")

    if fails:
        for f in fails:
            print(f"SELF-TEST FAIL: {f}")
        return 1
    print("runner_groups self-test: ok (ref guard, token guard, planner "
          "funds by priority, reports scarcity, and dry-run acts on nothing)")
    return 0
