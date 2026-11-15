#!/usr/bin/env python3
"""awrun.swarm -- N single tasks, N small models, N hosted runners, one report.

A swarm is a list of narrowly scoped tasks. Each task gets its own CI job: the job
clones ONE public repository, starts a small local model (llama.cpp on the runner's
CPU) and runs this loop against it. The loop has READ-ONLY tools over the clone
(list_dir, read_file, grep), a hard step budget, and must end with a `final` answer.
A task may carry a `check` regex; the answer is then graded PASS/FAIL here, so the
caller receives a verdict and not just prose. `--combine` merges every task's result
into one report, which the queue collects (see `awrun swarm submit`).

This file is self-contained and standard-library only ON PURPOSE: a runner executes
it straight from a checkout (`python3 awrun/swarm.py ...`) with nothing installed.

    swarm.py --list tasks.json                      # ids as a JSON array (the CI matrix)
    swarm.py --prepare tasks.json --id t1 --dest target   # clone + fetch the task's context
    swarm.py --task tasks.json --id t1 --root target --url http://127.0.0.1:8090 --out r.json
    swarm.py --combine parts/*.json --expect tasks.json --out report.json
    swarm.py --self-test

Task: {"id": "...", "goal": "...", "repo": "owner/name", "check": "<regex, optional>",
       "fetch": ["issues", "pulls"], "max_steps": 12}   (a list, or {"tasks": [...]})

`fetch` asks the job to write public GitHub data into the clone before the model
starts, because a model with file tools cannot call an API: `issues` becomes
`.swarm/issues.md` (open issues) and `pulls` becomes `.swarm/merged_pulls.md`.

Protocol, chosen so a 2B-4B model can follow it without native tool calling: every
reply is ONE JSON object, either {"tool": "<name>", "args": {...}} or
{"final": "<answer>"}. A reply that is not parseable JSON costs a step and is answered
with the error, never silently skipped.

Verdicts. PASS / FAIL: the answer was graded by the task's `check`. ANSWERED: an answer
with no check; nobody has verified it. UNGROUNDED: the model answered without one
successful tool call, so the answer was not read from the repository and is not graded.
BUDGET_EXHAUSTED: no usable answer inside the step budget (an empty, null or non-string
`final` is not an answer). ERROR: the model server failed. Everything except PASS and
ANSWERED is a failure in the report.

The repository listing shown in the first message is not a tool call: a task that
could be answered from it alone still has to read something, or it is UNGROUNDED.

A `check` must pin the expected value. One that also matches a placeholder ("0.0.0",
"1.2.3", "unknown", ...) is refused: on the first real run a version-shaped regex graded
an invented "1.0.0" as PASS. Read the true value before dispatch and write it in. "yes" and "no" are
placeholders too, so a yes/no question cannot carry a check: phrase the task so the
answer names the thing ("Apache-2.0: agree"), or leave it unchecked (ANSWERED).

Exit: 0 done, 1 a task is missing from the report / budget exhausted, 2 could not run.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

SYSTEM = (
    "You solve ONE narrowly scoped task about a code repository, using read-only tools.\n"
    "Reply with exactly ONE JSON object and nothing else:\n"
    '  {"tool": "list_dir", "args": {"path": "<dir>"}}\n'
    '  {"tool": "read_file", "args": {"path": "<file>", "start": 1, "lines": 80}}\n'
    '  {"tool": "grep", "args": {"pattern": "<regex>", "path": "<dir or file>"}}\n'
    '  {"final": "<your answer>"}\n'
    "Paths are relative to the repository root, for example `pyproject.toml` or "
    "`src/app.py`; never start one with `/`.\n"
    "Look before you answer, and answer ONLY from what a tool returned: if a tool "
    "returned an error, fix the call and try again instead of guessing.\n"
    "Answer briefly and exactly what was asked."
)
MAX_OBS = 4000          # characters of tool output fed back per step
GREP_HITS = 40
MAX_INLINE = 60_000     # a workflow_dispatch input is capped near 65 KB
MAX_TASKS = 32          # one job each: the cap on what a single dispatch can start
MAX_STEPS = 20          # model turns per task
MAX_TOKENS = 2048       # tokens the model may write per turn
#: A whole answer that says nothing: treated as an empty `final`.
NON_ANSWERS = frozenset({"", "null", "none", "n/a", "unknown"})
FAILURES = frozenset({"FAIL", "UNGROUNDED", "BUDGET_EXHAUSTED", "ERROR"})
#: Strings a model writes when it has not looked. A check that accepts one of
#: them grades a guess as PASS, so such a check is refused.
DECOYS = ("0.0.0", "1.2.3", "9.9.9", "x.y.z", "unknown", "none", "null",
          "n/a", "yes", "no", "")
FETCHABLE = ("issues", "pulls")
SIZES = ("2B", "4B", "9B")
REPO_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9_.-]+$")
ID_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
SKIP_DIRS = frozenset({".git", "node_modules", "__pycache__", ".venv"})


# ── the loop ────────────────────────────────────────────────────────────────

def _safe(root: Path, rel: str) -> Path:
    p = (root / (rel or ".")).resolve()
    if p != root and root not in p.parents:
        raise ValueError(f"path escapes the task root: {rel}")
    return p


def _path_hint(root: Path, rel: Any) -> Optional[str]:
    """An error that names the right form, for a path a small model tends to invent:
    absolute (`/path/to/x`) or a template (`<file>`). None when the path looks usable."""
    text = str(rel or "")
    if not (text.startswith("/") or "path/to" in text or "<" in text or ">" in text):
        return None
    name = text.replace(chr(92), "/").rstrip("/").rsplit("/", 1)[-1].strip("<>")
    stripped = text.lstrip("/")
    for guess in (stripped, name):
        if not guess or "<" in guess:
            continue
        try:
            # Only ever suggest a path INSIDE the clone: `/../../etc/passwd` must not
            # be answered with a hint that confirms the file exists.
            inside = _safe(root, guess).exists()
        except (OSError, ValueError):
            inside = False
        if inside:
            return (f'error: {text!r} is not a path in this repository. Paths are relative '
                    f'to the repository root: use "{guess}".')
    return (f'error: {text!r} is not a path in this repository. Paths are relative to the '
            'repository root, like "README.md". Call list_dir with path "." to see what '
            'exists.')


def run_tool(root: Path, name: str, args: Dict[str, Any]) -> str:
    """Execute one read-only tool. Errors come back as text for the model to read."""
    try:
        if name in ("list_dir", "read_file", "grep") and "path" in args:
            hint = _path_hint(root, args.get("path"))
            if hint:
                return hint
        if name == "list_dir":
            p = _safe(root, args.get("path", "."))
            # A symlink is NAMED, never followed: `@` marks it and no tool reads it.
            names = sorted(x.name + ("@" if x.is_symlink() else "/" if x.is_dir() else "")
                           for x in p.iterdir() if x.name != ".git")
            return "\n".join(names[:200]) or "(empty)"
        if name == "read_file":
            p = _safe(root, args["path"])
            start = max(int(args.get("start", 1)), 1)
            n = min(max(int(args.get("lines", 80)), 1), 200)
            text = p.read_text(encoding="utf-8", errors="replace").splitlines()
            chunk = text[start - 1:start - 1 + n]
            out = "\n".join(f"{start + i}: {ln}" for i, ln in enumerate(chunk))
            if out and start - 1 + n < len(text):
                out += f"\n({len(text)} lines in total)"
            return out or "(no lines)"
        if name == "grep":
            rx = re.compile(args["pattern"])
            base = _safe(root, args.get("path", "."))
            files = [base] if base.is_file() else sorted(base.rglob("*"))
            hits: List[str] = []
            for f in files:
                if SKIP_DIRS.intersection(f.relative_to(root).parts):
                    continue
                # A symlink committed in the repository can point anywhere on the
                # runner; skip every one, and anything reached through one.
                if f.is_symlink() or not f.is_file() or f.stat().st_size > 2_000_000:
                    continue
                real = f.resolve()
                if real != root and root not in real.parents:
                    continue
                try:
                    for i, ln in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                        if rx.search(ln):
                            rel = f.relative_to(root).as_posix()
                            hits.append(f"{rel}:{i}: {ln.strip()[:200]}")
                            if len(hits) >= GREP_HITS:
                                return "\n".join(hits) + "\n(truncated)"
                except UnicodeDecodeError:
                    continue
            return "\n".join(hits) or "(no matches)"
        return f"error: unknown tool {name!r} (use list_dir, read_file, grep or final)"
    except (OSError, ValueError, KeyError, TypeError, re.error) as exc:
        return f"error: {type(exc).__name__}: {exc}"


def parse_reply(text: str) -> Optional[Dict[str, Any]]:
    """The first JSON object in the reply, after any <think> block; None if absent."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    start = text.find("{")
    while start >= 0:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:i + 1])
                        if isinstance(obj, dict):
                            return obj
                    except json.JSONDecodeError:
                        break
                    break
        start = text.find("{", start + 1)
    return None


def http_chat(url: str, model: str, timeout: int, max_tokens: int) -> Callable:
    """A chat function over an OpenAI-compatible endpoint. Returns (text, stats):
    stats carries what the server measured (tokens out, seconds generating), so
    a report can state tokens per second instead of guessing it from wall time."""
    def chat(messages: List[Dict[str, str]]) -> Tuple[str, Dict[str, float]]:
        req = urllib.request.Request(
            url.rstrip("/") + "/v1/chat/completions",
            data=json.dumps({"model": model, "messages": messages, "temperature": 0,
                             "max_tokens": max_tokens}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read())
        usage = body.get("usage") or {}
        timings = body.get("timings") or {}
        stats = {
            "tokens_in": float(timings.get("prompt_n") or usage.get("prompt_tokens") or 0),
            "tokens_out": float(timings.get("predicted_n")
                                or usage.get("completion_tokens") or 0),
            "gen_s": float(timings.get("predicted_ms") or 0) / 1000.0,
            "prompt_s": float(timings.get("prompt_ms") or 0) / 1000.0,
        }
        return (body["choices"][0]["message"].get("content") or ""), stats
    return chat


def solve(task: Dict[str, Any], chat: Callable, root: Path,
          log: Callable[[str], None] = print) -> Dict[str, Any]:
    t0 = time.time()
    steps: List[Dict[str, Any]] = []
    totals = {"tokens_in": 0.0, "tokens_out": 0.0, "gen_s": 0.0, "prompt_s": 0.0}
    # The root listing is given up front: on the first real run a 2B model asked for
    # `/path/to/pyproject.toml`, got an error and answered from imagination.
    listing = run_tool(root, "list_dir", {"path": "."})
    msgs = [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"Task: {task['goal']}\n\nThe repository root "
                                        f"contains:\n{listing[:1500]}"}]
    answer: Optional[str] = None
    grounded = 0            # tool calls that returned something other than an error
    budget = int(task.get("max_steps", 12))
    for n in range(1, budget + 1):
        got = chat(msgs)
        reply, stats = got if isinstance(got, tuple) else (got, {})
        for k in totals:
            totals[k] += float(stats.get(k) or 0)
        msgs.append({"role": "assistant", "content": reply})
        act = parse_reply(reply)
        if act is None:
            obs = ('error: reply was not one JSON object. '
                   'Reply with {"tool": ...} or {"final": ...}.')
            steps.append({"n": n, "kind": "unparsed"})
        elif "final" in act and (not isinstance(act["final"], str)
                                 or act["final"].strip().lower() in NON_ANSWERS):
            # Empty, null or not a string: not an answer. Say so; the model may retry.
            obs = ('error: "final" must be a non-empty string holding your whole answer '
                   '(a table goes inside the string).')
            steps.append({"n": n, "kind": "empty_final"})
        elif "final" not in act and not act.get("tool"):
            obs = ('error: the object had neither "tool" nor "final". To answer, reply '
                   '{"final": "<your whole answer as one string>"}.')
            steps.append({"n": n, "kind": "no_action"})
        elif "final" in act:
            answer = act["final"]
            steps.append({"n": n, "kind": "final"})
            log(f"[progress] {task['id']} step {n}/{budget} final")
            break
        else:
            name = str(act.get("tool", ""))
            args = act.get("args") if isinstance(act.get("args"), dict) else {}
            obs = run_tool(root, name, args)
            ok = not obs.startswith("error:")
            grounded += 1 if ok else 0
            steps.append({"n": n, "kind": "tool", "tool": name, "args": args, "ok": ok})
        what = steps[-1].get("tool") or steps[-1]["kind"]
        log(f"[progress] {task['id']} step {n}/{budget} {what}")
        msgs.append({"role": "user", "content": "Observation:\n" + obs[:MAX_OBS]})
    check = task.get("check")
    if answer is None:
        verdict = "BUDGET_EXHAUSTED"
    elif grounded == 0:
        # Answered without reading anything: whatever it says, it was not looked up.
        verdict = "UNGROUNDED"
    elif check:
        verdict = "PASS" if re.search(check, answer, flags=re.I | re.S) else "FAIL"
    else:
        verdict = "ANSWERED"
    tok_s = round(totals["tokens_out"] / totals["gen_s"], 2) if totals["gen_s"] > 0 else None
    return {"id": task["id"], "goal": task["goal"], "repo": task.get("repo"),
            "answer": answer, "verdict": verdict, "check": check, "steps": len(steps),
            "tool_calls_ok": grounded, "trace": steps, "wall_s": round(time.time() - t0, 1),
            "tokens_in": int(totals["tokens_in"]), "tokens_out": int(totals["tokens_out"]),
            "gen_s": round(totals["gen_s"], 1), "tok_per_s": tok_s,
            "runner": os.environ.get("RUNNER_NAME") or None}


# ── the task list ───────────────────────────────────────────────────────────

def validate_tasks(doc: Any, allow_owner: str = "") -> List[Dict[str, Any]]:
    """The task list, or ValueError naming the first thing wrong with it.

    Run by the caller BEFORE dispatch and by the job again: a malformed list
    would otherwise fail on a runner minutes later, once per task."""
    tasks = doc.get("tasks", doc) if isinstance(doc, dict) and "goal" not in doc else doc
    tasks = tasks if isinstance(tasks, list) else [tasks]
    if not tasks:
        raise ValueError("the task list is empty")
    if len(tasks) > MAX_TASKS:
        raise ValueError(f"{len(tasks)} tasks; one swarm runs at most {MAX_TASKS} "
                         "(each is a CI job). Split it.")
    seen = set()
    for t in tasks:
        if not isinstance(t, dict):
            raise ValueError("every task is a JSON object")
        tid = t.get("id")
        if not isinstance(tid, str) or not ID_RX.match(tid):
            raise ValueError(f"task id {tid!r}: use 1-64 letters, digits, '.', '_' or '-' "
                             "(it names a CI job and a file)")
        if tid in seen:
            raise ValueError(f"task id {tid!r} appears twice")
        seen.add(tid)
        if not isinstance(t.get("goal"), str) or not t["goal"].strip():
            raise ValueError(f"task {tid}: a goal is required")
        repo = t.get("repo")
        if repo is not None:
            if not isinstance(repo, str) or not REPO_RX.match(repo) or ".." in repo:
                raise ValueError(f"task {tid}: repo must be owner/name, got {repo!r}")
            # Fails CLOSED: with no owner named, no repository is allowed at all.
            if not allow_owner or repo.split("/", 1)[0].lower() != allow_owner.lower():
                raise ValueError(f"task {tid}: repo {repo} is outside "
                                 f"{allow_owner or '(no owner was named)'}; a swarm only "
                                 "reads the repositories of the owner it runs under")
        fetch = t.get("fetch") or []
        if not isinstance(fetch, list) or any(f not in FETCHABLE for f in fetch):
            raise ValueError(f"task {tid}: fetch takes a list of {list(FETCHABLE)}")
        if fetch and not repo:
            raise ValueError(f"task {tid}: fetch needs a repo")
        steps = t.get("max_steps", 12)
        if (not isinstance(steps, int) or isinstance(steps, bool)
                or not 1 <= steps <= MAX_STEPS):
            raise ValueError(f"task {tid}: max_steps must be 1-{MAX_STEPS}")
        check = t.get("check") or ""
        if not isinstance(check, str):
            raise ValueError(f"task {tid}: check is a regex string")
        # A control character in a check is an escaping bug, never intent: JSON "\b"
        # is BACKSPACE, so a regex written one level under-escaped can never match
        # and grades a correct answer FAIL.
        bad = [c for c in check if ord(c) < 32 and c not in "\t\n"]
        if bad:
            raise ValueError(f"task {tid}: check contains control character(s) "
                             f"{[hex(ord(c)) for c in bad]} -- under-escaped regex? "
                             "JSON needs \\\\b for a word boundary")
        try:
            rx = re.compile(check, flags=re.I | re.S)
        except re.error as exc:
            raise ValueError(f"task {tid}: check is not a valid regex: {exc}") from exc
        if check:
            loose = next((d for d in DECOYS if rx.search(d)), None)
            if loose is not None:
                raise ValueError(f"task {tid}: check is too loose, it accepts the "
                                 f"placeholder {loose!r}. Pin the expected value (read it "
                                 "before dispatch), or drop the check.")
    return tasks


def load_tasks(path: str, allow_owner: str = "") -> List[Dict[str, Any]]:
    return validate_tasks(json.loads(Path(path).read_text(encoding="utf-8")), allow_owner)


def inline(tasks: List[Dict[str, Any]]) -> str:
    """The task list as one workflow input. ValueError when it cannot travel inline."""
    body = json.dumps({"tasks": tasks}, separators=(",", ":"))
    if len(body) > MAX_INLINE:
        raise ValueError(f"task list is {len(body)} characters; a workflow input holds "
                         f"about {MAX_INLINE}. Split the swarm.")
    return body


# ── the boundary: what may leave this machine for a shared runner ───────────

#: A hosted runner is shared with strangers and its logs are public, so a task list
#: may carry nothing a stranger must not read. These are the generic classes; a host
#: adds its own (internal names, ticket ids) with `extra` patterns.
_BOUNDARY = (
    ("api-key", r"\b(sk|pk|rk)[-_](live|test|ant|proj)?[-_]?[A-Za-z0-9]{20,}"),
    ("github-token", r"\b(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    ("cloud-key", r"\bAKIA[0-9A-Z]{16}\b"),
    ("chat-token", r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    ("private-key", "-----BEGIN" + r" [A-Z ]*PRIVATE KEY"),
    ("bearer", r"(?i)\b(bearer|authorization:)\s+[A-Za-z0-9._~+/-]{20,}"),
    ("windows-path", r"\b[A-Za-z]:[\\/][A-Za-z0-9_.-]"),
    ("home-path", r"(?<![\w.])/(home|root|Users|mnt|opt/[a-z])[/\w]"),
    ("private-address", r"\b(10|127|192\.168|172\.(1[6-9]|2\d|3[01]))\.\d+\.\d+"
                        r"(\.\d+)?\b"),
)


def boundary_hits(tasks: List[Dict[str, Any]],
                  extra: Optional[List[Tuple[str, str]]] = None) -> List[str]:
    """Why this task list may NOT be sent to a shared runner: one line per finding,
    naming the task and the class, never echoing the matched text (it may be the
    secret). Empty = it may go. The caller REFUSES on any hit; nothing is redacted
    and sent anyway, because a scrubbed payload still ran."""
    rules = [(n, re.compile(rx)) for n, rx in list(_BOUNDARY) + list(extra or [])]
    out: List[str] = []
    for t in tasks:
        # The values themselves, not their JSON spelling: JSON doubles a backslash.
        text = " ".join(str(v) for v in t.values())
        for name, rx in rules:
            if rx.search(text):
                out.append(f"task {t.get('id')}: carries a {name}")
    return out


def load_patterns(path: str) -> List[Tuple[str, str]]:
    """Extra boundary patterns from a file: `name<TAB or spaces>regex` per line,
    `#` comments. A pattern that does not compile is an error, never skipped."""
    out: List[Tuple[str, str]] = []
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            raise ValueError(f"{path}:{n}: want `name regex`")
        try:
            re.compile(parts[1])
        except re.error as exc:
            raise ValueError(f"{path}:{n}: {exc}") from exc
        out.append((parts[0], parts[1]))
    return out


# ── the task's context (runs on the job, before the model starts) ───────────

def _api(path: str, token: str = "") -> Any:
    req = urllib.request.Request(
        "https://api.github.com" + path,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "awrun-swarm"})
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def _one_line(text: Any, limit: int = 160) -> str:
    return re.sub(r"\s+", " ", str(text or "")).replace("|", "/").strip()[:limit]


def render_issues(items: List[Dict[str, Any]]) -> str:
    rows = [i for i in items if "pull_request" not in i]
    out = [f"# Open issues ({len(rows)})", "",
           "| number | title | labels | opened | comments |", "|---|---|---|---|---|"]
    for i in rows:
        labels = ", ".join(_one_line(lb.get("name"), 30) for lb in i.get("labels") or [])
        out.append(f"| #{i.get('number')} | {_one_line(i.get('title'))} | {labels or '-'} | "
                   f"{str(i.get('created_at') or '')[:10]} | {i.get('comments', 0)} |")
    if not rows:
        out.append("| - | (no open issues) | - | - | - |")
    return "\n".join(out) + "\n"


def render_pulls(items: List[Dict[str, Any]]) -> str:
    rows = [p for p in items if p.get("merged_at")]
    rows.sort(key=lambda p: str(p.get("merged_at")), reverse=True)
    out = [f"# Recently merged pull requests ({len(rows)})", "",
           "| number | title | merged |", "|---|---|---|"]
    for p in rows:
        out.append(f"| #{p.get('number')} | {_one_line(p.get('title'))} | "
                   f"{str(p.get('merged_at'))[:10]} |")
    if not rows:
        out.append("| - | (no merged pull requests) | - |")
    return "\n".join(out) + "\n"


def prepare(task: Dict[str, Any], dest: Path, token: str = "",
            api: Callable[[str, str], Any] = _api,
            clone: Optional[Callable[[str, Path], None]] = None) -> Path:
    """Clone the task's repository into `dest` and write its `fetch` context there.
    A task with no repo gets an empty directory: the caller checks one out itself."""
    repo = task.get("repo")
    if not repo:
        dest.mkdir(parents=True, exist_ok=True)
        return dest

    def _clone(url: str, into: Path) -> None:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.run(["git", "clone", "--depth", "1", "--quiet", url, str(into)],
                       check=True, timeout=600, creationflags=flags)

    (clone or _clone)(f"https://github.com/{repo}.git", dest)
    fetch = task.get("fetch") or []
    if fetch:
        ctx = dest / ".swarm"
        ctx.mkdir(exist_ok=True)
        if "issues" in fetch:
            items = api(f"/repos/{repo}/issues?state=open&per_page=50", token)
            (ctx / "issues.md").write_text(render_issues(items), encoding="utf-8")
        if "pulls" in fetch:
            items = api(f"/repos/{repo}/pulls?state=closed&sort=updated&direction=desc"
                        "&per_page=40", token)
            (ctx / "merged_pulls.md").write_text(render_pulls(items), encoding="utf-8")
    return dest


# ── the report ──────────────────────────────────────────────────────────────

def combine(paths: List[str], expect: Optional[List[str]] = None,
            meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """One report from every task's result. A task in `expect` that reported
    nothing is listed under `missing` -- a swarm that lost a job must say so,
    because nine answers out of ten read exactly like ten out of ten."""
    rows, seen = [], set()
    for p in paths:
        r = json.loads(Path(p).read_text(encoding="utf-8"))
        if r["id"] in seen:
            raise ValueError(f"task {r['id']} reported twice")
        seen.add(r["id"])
        rows.append(r)
    counts: Dict[str, int] = {}
    for r in rows:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    graded = [r for r in rows if r.get("check")]
    passed = sum(1 for r in graded if r["verdict"] == "PASS")
    rates = [r["tok_per_s"] for r in rows if r.get("tok_per_s")]
    missing = sorted(set(expect or []) - seen)
    report: Dict[str, Any] = {
        "tasks": len(rows), "missing": missing, "verdicts": counts,
        "failures": sum(n for v, n in counts.items() if v in FAILURES) + len(missing),
        "ungrounded": counts.get("UNGROUNDED", 0),
        "graded": len(graded),
        "pass_rate": round(passed / len(graded), 4) if graded else None,
        "tok_per_s": {"min": min(rates), "max": max(rates),
                      "mean": round(sum(rates) / len(rates), 2)} if rates else None,
        "tokens_out": sum(int(r.get("tokens_out") or 0) for r in rows),
        "task_wall_s": {"max": max(r.get("wall_s") or 0 for r in rows),
                        "sum": round(sum(r.get("wall_s") or 0 for r in rows), 1)}
        if rows else None,
        "results": sorted(rows, key=lambda r: r["id"])}
    report.update(meta or {})
    return report


def render_report(report: Dict[str, Any]) -> str:
    """The report as Markdown: what a person, or the agent that asked, reads."""
    tps = report.get("tok_per_s") or {}
    head = [f"## swarm report: {report.get('tasks', 0)} task(s)"
            + (f", model {report['model']}" if report.get("model") else ""), ""]
    head.append(f"- verdicts: {report.get('verdicts')}")
    head.append(f"- failures: {report.get('failures', 0)} (FAIL, UNGROUNDED, "
                "BUDGET_EXHAUSTED, ERROR or missing). ANSWERED means unverified.")
    head.append(f"- ungrounded: {report.get('ungrounded', 0)} (answered without one "
                "successful tool call; never graded)")
    if report.get("pass_rate") is not None:
        head.append(f"- graded pass rate: {report['pass_rate']} of {report.get('graded')}")
    if tps:
        head.append(f"- tokens/s per runner: min {tps['min']}, mean {tps['mean']}, "
                    f"max {tps['max']}")
    if report.get("missing"):
        head.append(f"- MISSING (no result): {', '.join(report['missing'])}")
    rows = ["", "| task | repo | verdict | steps | tok/s | wall s | answer |",
            "|---|---|---|---|---|---|---|"]
    for r in report.get("results") or []:
        rows.append(f"| {r['id']} | {r.get('repo') or '-'} | {r['verdict']} | "
                    f"{r.get('steps')} | {r.get('tok_per_s') or '-'} | {r.get('wall_s')} | "
                    f"{_one_line(r.get('answer'), 300)} |")
    return "\n".join(head + rows) + "\n"


# ── self-test ───────────────────────────────────────────────────────────────

def self_test() -> int:
    import tempfile
    fails: List[str] = []
    root = Path(tempfile.mkdtemp()).resolve()
    (root / "cfg.yaml").write_text("service: api\nport: 8001\n", encoding="utf-8")
    script = iter(['<think>look first</think>'
                   '{"tool": "grep", "args": {"pattern": "port", "path": "."}}',
                   "not json at all",
                   ('{"final": "The port is 8001."}',
                    {"tokens_out": 30.0, "gen_s": 2.0})])
    r = solve({"id": "t1", "goal": "port?", "check": r"\b8001\b"}, lambda m: next(script),
              root, log=lambda s: None)
    if r["verdict"] != "PASS" or r["steps"] != 3 or r["trace"][1]["kind"] != "unparsed":
        fails.append(f"happy path: {r}")
    if r["tok_per_s"] != 15.0:
        fails.append(f"tokens per second not taken from the server's stats: {r['tok_per_s']}")
    looked = '{"tool": "read_file", "args": {"path": "cfg.yaml"}}'
    wrong_script = iter([looked, '{"final": "9000"}'])
    wrong = solve({"id": "t2", "goal": "port?", "check": r"\b8001\b"},
                  lambda m: next(wrong_script), root, log=lambda s: None)
    if wrong["verdict"] != "FAIL":
        fails.append("a wrong answer was not graded FAIL")
    guess_script = iter(['{"tool": "read_file", "args": {"path": "/path/to/cfg.yaml"}}',
                         '{"final": "The port is 8001."}'])
    hints: List[str] = []

    def _guess(m: List[Dict[str, str]]) -> str:
        hints.append(m[-1]["content"])
        return next(guess_script)

    guess = solve({"id": "t5", "goal": "port?", "check": r"\b8001\b"}, _guess, root,
                  log=lambda s: None)
    same_script = iter([looked, '{"final": "The port is 8001."}'])
    same = solve({"id": "t6", "goal": "port?", "check": r"\b8001\b"},
                 lambda m: next(same_script), root, log=lambda s: None)
    if same["verdict"] != "PASS":
        fails.append(f"the same answer after a successful tool call was {same['verdict']}")
    beside = root.parent / (root.name + "-beside.txt")
    beside.write_text("x", encoding="utf-8")
    if 'use "' in run_tool(root, "read_file", {"path": "/../" + beside.name}):
        fails.append("a path hint suggested a file that exists OUTSIDE the root")
    if 'use "cfg.yaml"' not in run_tool(root, "read_file", {"path": "/cfg.yaml"}):
        fails.append("a path hint did not suggest the file inside the root")
    if guess["verdict"] != "UNGROUNDED":
        fails.append(f"an answer given after the only tool call errored was graded "
                     f"{guess['verdict']}; it was never looked up")
    if '"cfg.yaml"' not in hints[-1]:
        fails.append(f"an invented absolute path was not answered with the relative "
                     f"form: {hints[-1]}")
    loop = solve({"id": "t3", "goal": "x", "max_steps": 3},
                 lambda m: '{"tool": "list_dir", "args": {}}', root, log=lambda s: None)
    if loop["verdict"] != "BUDGET_EXHAUSTED" or loop["steps"] != 3:
        fails.append("budget was not enforced")
    seen: List[str] = []
    retry = iter([looked, '{"final": null}', '{"number": 1, "summary": "x"}',
                  '{"final": "null"}', '{"final": {"a": 1}}', '{"final": " N/A "}',
                  '{"final": "Unknown"}', '{"final": "done"}'])

    def _chat(m: List[Dict[str, str]]) -> str:
        seen.append(m[-1]["content"])
        return next(retry)

    again = solve({"id": "t4", "goal": "x", "max_steps": 10}, _chat, root,
                  log=lambda s: None)
    if again["answer"] != "done" or [t["kind"] for t in again["trace"]] != [
            "tool", "empty_final", "no_action", "empty_final", "empty_final",
            "empty_final", "empty_final", "final"]:
        fails.append(f"an empty or shapeless reply was taken as the answer: {again}")
    if "cfg.yaml" not in seen[0]:
        fails.append("the first message does not show the repository root")
    if "escapes" not in run_tool(root, "read_file", {"path": "../../etc/passwd"}):
        fails.append("a path outside the root was readable")
    parts = []
    for res in (r, wrong):
        p = root / f"{res['id']}.json"
        p.write_text(json.dumps(res), encoding="utf-8")
        parts.append(str(p))
    rep = combine(parts, expect=["t1", "t2", "t9"])
    if rep["pass_rate"] != 0.5 or rep["verdicts"] != {"PASS": 1, "FAIL": 1}:
        fails.append(f"combine: {rep}")
    if combine(parts)["ungrounded"] != 0:
        fails.append("ungrounded count is wrong")
    if rep["failures"] != 2:
        fails.append(f"a FAIL and a missing task are two failures, got {rep['failures']}")
    if rep["missing"] != ["t9"]:
        fails.append("a task that reported nothing was not listed as missing")
    try:
        combine([parts[0], parts[0]])
        fails.append("a duplicate task was merged")
    except ValueError as exc:
        if "twice" not in str(exc):
            fails.append(f"duplicate refused for the wrong reason: {exc}")
    bs = chr(92)
    ok = [{"id": "a", "goal": "g", "repo": "Aitherium/awrun", "check": bs + "b22" + bs + "b"}]
    outside = root.parent / (root.name + "-outside.txt")
    outside.write_text("port 9999 secret", encoding="utf-8")
    try:
        (root / "link.txt").symlink_to(outside)
        if "9999" in run_tool(root, "grep", {"pattern": "port", "path": "."}):
            fails.append("grep read through a symlink that leaves the root")
        if "escapes" not in run_tool(root, "read_file", {"path": "link.txt"}):
            fails.append("read_file followed a symlink that leaves the root")
        if "link.txt@" not in run_tool(root, "list_dir", {"path": "."}):
            fails.append("list_dir did not mark a symlink")
        (root / "link.txt").unlink()
    except OSError:
        pass        # this platform cannot make a symlink; nothing to follow either
    if validate_tasks({"tasks": ok}, "aitherium")[0]["id"] != "a":
        fails.append("a valid task list did not load")
    refusals = [
        ([{"id": "a", "goal": "g", "check": chr(8) + "22" + chr(8)}], "", "control character"),
        ([{"id": "a", "goal": "g", "repo": "someone/else"}], "Aitherium", "outside"),
        ([{"id": "a", "goal": "g", "repo": "Aitherium/awrun"}], "", "no owner was named"),
        ([{"id": f"t{i}", "goal": "g"} for i in range(MAX_TASKS + 1)], "", "at most"),
        ([{"id": "a", "goal": "g", "max_steps": MAX_STEPS + 1}], "", "max_steps"),
        ([{"id": "a", "goal": "g", "repo": "../etc"}], "", "owner/name"),
        ([{"id": "a/b", "goal": "g"}], "", "task id"),
        ([{"id": "a", "goal": "g", "check": bs + "d+" + bs + "." + bs + "d+" + bs + "."
           + bs + "d+"}], "", "too loose"),
        ([{"id": "a", "goal": "g", "fetch": ["secrets"], "repo": "o/r"}], "o", "fetch"),
        ([{"id": "a", "goal": "g"}, {"id": "a", "goal": "h"}], "", "twice"),
    ]
    for doc, owner, why in refusals:
        try:
            validate_tasks(doc, owner)
            fails.append(f"accepted a task list that should be refused ({why})")
        except ValueError as exc:
            if why not in str(exc):
                fails.append(f"refused for the wrong reason: wanted {why!r}, got {exc}")
    made = prepare({"id": "p", "goal": "g", "repo": "o/r", "fetch": ["issues", "pulls"]},
                   root / "clone",
                   api=lambda path, tok: [
                       {"number": 3, "title": "a | b", "labels": [{"name": "bug"}],
                        "created_at": "2026-01-02T00:00:00Z", "comments": 1},
                       {"number": 4, "title": "pr", "pull_request": {}, "merged_at": None},
                       {"number": 5, "title": "merged", "merged_at": "2026-01-03T00:00:00Z"}],
                   clone=lambda url, into: into.mkdir())
    issues = (made / ".swarm" / "issues.md").read_text(encoding="utf-8")
    if "#3" not in issues or "#4" in issues or "a / b" not in issues:
        fails.append(f"issues context is wrong: {issues}")
    if "#5" not in (made / ".swarm" / "merged_pulls.md").read_text(encoding="utf-8"):
        fails.append("merged pull requests were not written")
    clean = [{"id": "c", "goal": "Which licence does pyproject.toml declare?"}]
    if boundary_hits(clean):
        fails.append(f"a clean task was refused at the boundary: {boundary_hits(clean)}")
    token = "gh" + "p_" + "a" * 36
    dirty = [{"id": "k", "goal": "use " + token}, {"id": "w", "goal": "read C:" + bs + "work"},
             {"id": "i", "goal": "see TICKET-1234"}]
    hits = boundary_hits(dirty, [("ticket-id", "TICKET-" + bs + "d+")])
    if len(hits) != 3 or any(token in h for h in hits):
        fails.append(f"boundary: wanted 3 findings that never echo the match, got {hits}")
    if main(["--list", "x.json", "--require-owner", "--allow-owner", ""]) != 2:
        fails.append("--require-owner with an empty owner did not stop the run")
    if "MISSING" not in render_report(rep) or "| t1 |" not in render_report(rep):
        fails.append("the rendered report does not name the missing task or the results")
    for f in fails:
        print("SELF-TEST FAIL:", f)
    print("self-test:", "FAIL" if fails else "PASS")
    return 1 if fails else 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("--task", help="task file; with --id picks one task from a list")
    ap.add_argument("--id", help="task id inside --task / --prepare")
    ap.add_argument("--list", metavar="TASKS", help="print task ids as a JSON array")
    ap.add_argument("--prepare", metavar="TASKS", help="clone the task's repo into --dest")
    ap.add_argument("--dest", default="target")
    ap.add_argument("--combine", nargs="*", metavar="RESULT.json")
    ap.add_argument("--expect", metavar="TASKS", help="with --combine: the list every task "
                                                      "of which must have reported")
    ap.add_argument("--meta", action="append", default=[], metavar="KEY=VALUE",
                    help="with --combine: a fact to record in the report (model=4B)")
    ap.add_argument("--markdown", help="with --combine: also write the report as Markdown")
    ap.add_argument("--allow-owner", default="",
                    help="the only owner whose repositories a task may name; without "
                         "it every task that names a repository is refused")
    ap.add_argument("--require-owner", action="store_true",
                    help="exit 2 when --allow-owner is empty (what a workflow passes: an "
                         "unset owner must stop the run, not lift the restriction)")
    ap.add_argument("--root", help="the tree the tools may read (default: task.root or cwd)")
    ap.add_argument("--url", default="http://127.0.0.1:8090")
    ap.add_argument("--model", default="bonsai2")
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--max-tokens", type=int, default=MAX_TOKENS)
    ap.add_argument("--out", help="write the result / report here")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        return self_test()
    if a.require_owner and not a.allow_owner.strip():
        print("could not run: --require-owner is set and --allow-owner is empty",
              file=sys.stderr)
        return 2
    try:
        if a.list:
            print(json.dumps([t["id"] for t in load_tasks(a.list, a.allow_owner)]))
            return 0
        if a.combine is not None:
            expect = ([t["id"] for t in load_tasks(a.expect, a.allow_owner)]
                      if a.expect else None)
            meta = dict(kv.split("=", 1) for kv in a.meta if "=" in kv)
            rep = combine(a.combine, expect, meta)
            text = render_report(rep)
            print(text)
            if a.out:
                Path(a.out).write_text(json.dumps(rep, indent=2), encoding="utf-8")
            if a.markdown:
                Path(a.markdown).write_text(text, encoding="utf-8")
            return 1 if rep["missing"] else 0
        source = a.prepare or a.task
        if not source:
            ap.error("one of --list, --prepare, --task, --combine or --self-test is required")
        tasks = load_tasks(source, a.allow_owner)
        task = next((t for t in tasks if t["id"] == a.id), None) if a.id else tasks[0]
        if task is None:
            print(f"no task {a.id!r} in {source}", file=sys.stderr)
            return 2
        if a.prepare:
            dest = prepare(task, Path(a.dest), os.environ.get("GH_TOKEN", ""))
            print(f"prepared {task['id']}: {task.get('repo') or '(no repo)'} -> {dest}")
            return 0
    except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError,
            urllib.error.URLError) as exc:
        print(f"could not run: {exc}", file=sys.stderr)
        return 2
    root = Path(a.root or task.get("root") or os.getcwd()).resolve()
    try:
        res = solve(task, http_chat(a.url, a.model, a.timeout,
                                    min(max(a.max_tokens, 1), MAX_TOKENS)), root)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        # The model server died or answered garbage: a result still exists, so the
        # report shows this task as an error instead of losing it.
        res = {"id": task["id"], "goal": task["goal"], "repo": task.get("repo"),
               "answer": None, "verdict": "ERROR", "check": task.get("check"), "steps": 0,
               "trace": [], "wall_s": 0, "error": f"{type(exc).__name__}: {exc}"[:500],
               "runner": os.environ.get("RUNNER_NAME") or None}
    print(json.dumps({k: v for k, v in res.items() if k != "trace"}, indent=2), flush=True)
    if a.out:
        Path(a.out).write_text(json.dumps(res, indent=2), encoding="utf-8")
    return 0 if res["verdict"] in ("PASS", "ANSWERED") else 1


if __name__ == "__main__":
    sys.exit(main())
