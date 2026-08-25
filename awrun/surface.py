"""One view of CI across every public repo, and one lever to drive them all.

WHY THIS IS DISPATCH AND NOT HOSTING
------------------------------------
The obvious version of "run our builds across the whole public surface without
burning our own CPUs" is to point the public repos at our self-hosted runners.
Measured 2026-08-24, that is backwards on both halves:

* **They already do not burn our CPUs.** Every public repo here runs on
  GitHub-HOSTED runners -- free and unlimited for public repositories, macOS and
  Windows included. awdk's latest run used three of them and passed. Moving that
  onto self-hosted hardware replaces free compute with compute we pay for.
* **It would open the one hole GitHub guards by default.** All three runner
  groups on this org carry ``allows_public_repositories=false``. On a public
  repo, anyone can open a pull request, and its workflow runs on the runner --
  arbitrary code on a box that holds credentials and sits inside our VPC. That
  flag is not an oversight to switch off; it is the guard.

So the leverage is not WHERE the jobs run. It is that nothing could see or drive
the 40 repos AT ONCE. Measured the same day: **9 public repos have no CI at all**
(``awnix`` -- the runner OS -- among them), and 3 were red, one of which had been
published hours earlier. Nobody knew, because knowing meant opening 40 tabs.

This module is that missing view, and the fan-out that acts on it. It spends
nothing: every job it starts lands on the free hosted pool.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

#: Workflows GitHub runs on its own; they say nothing about whether a project
#: tests itself, and counting them would report full coverage on a repo that has
#: never run a test.
NOT_OUR_CI = ("pages-build-deployment", "Dependabot Updates")


def classify(repo: str, workflows: List[Dict[str, Any]],
             runs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """One repo's CI state. PURE -- no API calls, no clock.

    ``state`` is one of: no-ci, red, green, unknown. ``unknown`` is deliberate
    and distinct from green: a repo with a workflow that has never run has not
    passed anything, and folding it into green is how a surface report says
    "all clear" about a project nobody has ever built.
    """
    ours = [w for w in workflows
            if w.get("name") not in NOT_OUR_CI
            and "pages-build" not in str(w.get("name", ""))]
    if not ours:
        return {"repo": repo, "state": "no-ci", "workflows": 0,
                "detail": "no workflow of ours -- nothing builds or tests this"}
    real = [r for r in runs if r.get("name") not in NOT_OUR_CI]
    if not real:
        return {"repo": repo, "state": "unknown", "workflows": len(ours),
                "detail": "has CI that has never run"}
    latest = real[0]
    concl = latest.get("conclusion")
    if concl == "failure":
        state = "red"
    elif concl in ("success", "skipped"):
        state = "green"
    else:
        state = "unknown"
    return {"repo": repo, "state": state, "workflows": len(ours),
            "detail": f"{latest.get('name', '?')}: {concl or latest.get('status')}"}


def summarise(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Counts plus the two lists worth acting on."""
    by: Dict[str, int] = {}
    for r in rows:
        by[r["state"]] = by.get(r["state"], 0) + 1
    return {"total": len(rows), "by_state": by,
            "no_ci": [r["repo"] for r in rows if r["state"] == "no-ci"],
            "red": [r["repo"] for r in rows if r["state"] == "red"]}


def dispatchable(workflows: List[Dict[str, Any]], want: str) -> Optional[int]:
    """The id of a workflow named/filed ``want``, or None.

    Matched on FILE NAME as well as display name: a fan-out addressed by display
    name silently skips every repo that spells the same workflow differently,
    and a skip is indistinguishable from a repo that does not have it.
    """
    for w in workflows:
        path = str(w.get("path", "")).rsplit("/", 1)[-1]
        if want in (w.get("name"), path, path.rsplit(".", 1)[0]):
            return w.get("id")
    return None


def self_test() -> int:
    bad = 0

    def check(cond: bool, what: str) -> None:
        nonlocal bad
        print(f"  {'ok  ' if cond else 'FAIL'} {what}")
        if not cond:
            bad += 1

    wf = [{"name": "CI", "path": ".github/workflows/ci.yml", "id": 7}]
    pages = [{"name": "pages-build-deployment", "path": "x", "id": 1}]

    check(classify("r", [], [])["state"] == "no-ci",
          "a repo with no workflows is no-ci")
    check(classify("r", pages, [])["state"] == "no-ci",
          "pages-build-deployment is GitHub's, not ours -- counting it would "
          "report coverage on a repo that has never run a test")
    check(classify("r", wf, [])["state"] == "unknown",
          "CI that has never run is UNKNOWN, not green -- it has passed nothing")
    check(classify("r", wf, [{"name": "CI", "conclusion": "failure"}])["state"]
          == "red", "a failing latest run is red")
    check(classify("r", wf, [{"name": "CI", "conclusion": "success"}])["state"]
          == "green", "a passing latest run is green")
    check(classify("r", wf, [{"name": "CI", "conclusion": None,
                              "status": "in_progress"}])["state"] == "unknown",
          "a run still in flight is unknown, never green")

    s = summarise([{"repo": "a", "state": "no-ci"}, {"repo": "b", "state": "red"},
                   {"repo": "c", "state": "green"}])
    check(s["no_ci"] == ["a"] and s["red"] == ["b"],
          "the summary names the repos to act on, not just counts")

    check(dispatchable(wf, "CI") == 7, "a workflow matches by display name")
    check(dispatchable(wf, "ci.yml") == 7, "...and by file name")
    check(dispatchable(wf, "ci") == 7, "...and by stem")
    check(dispatchable(wf, "release") is None,
          "an absent workflow is None, so a fan-out can SAY it skipped a repo "
          "rather than skipping it silently")

    print()
    if bad:
        print(f"surface self-test: {bad} case(s) FAILED")
        return 1
    print("surface self-test: all cases behaved correctly")
    return 0
