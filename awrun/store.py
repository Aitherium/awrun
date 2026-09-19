"""Durable, cross-process priority queue for agentic runs and ad-hoc CI builds.

Modeled directly on `awdk/adk/decisions/store.py` — that store already solved
the concurrency problem a run queue has (concurrent Claude Code sessions
racing to act on the same durable record) and is proven, load-bearing code in
this exact repo. Two design notes carried over unchanged, because they are
consequences of real constraints, not preference:

* **One file per item.** A single shared JSON document would make every
  submit/claim a read-modify-write against a file another process is also
  rewriting, and the loser's write vanishes silently.
* **Atomic replace, never in-place write.** A reader (the dispatcher, `awrun
  queue`) polls this directory. A partially-written file would read as
  corrupt JSON and the item would flicker out of the list and back.
  `os.replace` is atomic on both POSIX and Windows.

One thing is deliberately DIFFERENT from decisions/store.py, and it is the
reason this is not just a copy: **claiming an item is a directory move, not a
lock-guarded field write.** `decisions/store.py` protects `answer()` with a
`threading.RLock()` — real protection for one process, but two independent
processes (a CLI submit and a dispatcher loop, or two dispatcher instances)
racing to answer the same card can both pass the "is it still open" read
before either writes; the window is small, not zero. For a decision card,
answered by a human at human latency, that has never mattered. For a build
queue, where "exactly one worker claims this item" is the entire point,
it does. So status here is encoded by WHICH DIRECTORY the file lives in
(`queued/`, `claimed/`, `running/`, `done/`, `failed/`, `cancelled/`), and a
claim is `os.rename(queued/<id>.json, claimed/<id>.json)`. `os.rename` is
atomic and, when the source has already been moved by a competing claimant,
raises `FileNotFoundError` — the OS itself is the mutex, no lock file needed,
and it is correct across processes and across machines sharing the directory
over a network mount, which a Python-level lock is not.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

STATUS_QUEUED = "queued"
STATUS_CLAIMED = "claimed"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

#: Every status has a directory. Order matters for _all_statuses() only in
#: that it is deterministic, not that it means anything else.
ALL_STATUSES = (STATUS_QUEUED, STATUS_CLAIMED, STATUS_RUNNING,
                STATUS_DONE, STATUS_FAILED, STATUS_CANCELLED)
CLOSED_STATUSES = frozenset({STATUS_DONE, STATUS_FAILED, STATUS_CANCELLED})
OPEN_STATUSES = frozenset({STATUS_QUEUED, STATUS_CLAIMED, STATUS_RUNNING})

#: "comet-deploy" added for Phase 7 (the one cloud-facing kind: a thin
#: passthrough to AitherComet's own /deploy, which already tenant-scopes and
#: cost-gates -- see dispatcher.py's _run_comet_deploy).
#: `render` (2026-09-03): a media render claimed by a host-registered RunFn
#: (`awrun_render_worker.py`) -- the WebMCP Design Studio's render-video lane.
#: The queue knows nothing about rendering; it only carries the kind so several
#: renderer workers can claim from one queue.
#: `artpack` (2026-09-05): one Dark Matters character spec to bake into a
#: character_pack/ on an art node (`lib/compute/lambda_art_node.py` drains it).
#: Host-registered like `render`: the queue carries the item; the node driver
#: claims it. With no driver running, an `artpack` item waits in `queued/`.
#: `solve` (2026-09-06): one ProblemSpec (awpredict.contracts) to play through a
#: ProblemSession -- the general solver's unit of work. Host-registered like
#: `render`: `awgym.gym.awrun_solve.run_solve` is the RunFn; the queue only carries
#: the kind so the kernel/awsh can submit and any aitherd with awgym can claim.
KINDS = ("agent", "ci", "comet-deploy", "render", "artpack", "solve")

#: Kinds that touch a GPU. For these a `gpu` request is REQUIRED at submit --
#: an item that does not say what it needs cannot be admitted by a GPU lease
#: door, and "run it and see" is how a render walks onto a card with no free
#: memory and dies eleven minutes in. Other kinds MAY carry one.
GPU_KINDS = frozenset({"render", "artpack", "solve"})

#: The priority classes a lease door arbitrates between, most urgent first.
GPU_CLASSES = ("arc", "interactive_media", "chat", "training", "detection")

_GPU_REQUIRED_KEYS = ("class", "vram_mb")
_GPU_ALLOWED_KEYS = frozenset({"class", "vram_mb", "host_pref", "ttl_s", "backend"})
_GPU_DEFAULT_TTL_S = 600


def validate_gpu(kind: str, gpu: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Normalise a `gpu` request, or raise RunError saying exactly what is wrong.

    Shape: ``{"class": <GPU_CLASSES>, "vram_mb": int > 0, "host_pref": str = "auto",
    "ttl_s": int > 0 = 600}`` plus an optional ``"backend"`` name. Optional in
    storage (an old item on disk with no `gpu` still loads); required at submit
    for GPU_KINDS.
    """
    if gpu is None:
        if kind in GPU_KINDS:
            raise RunError(
                f"kind={kind!r} runs on a GPU and needs a gpu request: "
                f"gpu={{'class': one of {GPU_CLASSES}, 'vram_mb': <int>}} "
                f"(optional: host_pref, ttl_s, backend)")
        return None
    if not isinstance(gpu, dict):
        raise RunError(f"gpu must be a mapping, got {type(gpu).__name__}")
    unknown = sorted(set(gpu) - _GPU_ALLOWED_KEYS)
    if unknown:
        raise RunError(f"gpu has unknown keys {unknown}; allowed: {sorted(_GPU_ALLOWED_KEYS)}")
    missing = [k for k in _GPU_REQUIRED_KEYS if k not in gpu]
    if missing:
        raise RunError(f"gpu is missing required keys {missing}")
    cls = gpu["class"]
    if cls not in GPU_CLASSES:
        raise RunError(f"gpu.class must be one of {GPU_CLASSES}, got {cls!r}")
    out: dict[str, Any] = {"class": cls}
    for key, default in (("vram_mb", None), ("ttl_s", _GPU_DEFAULT_TTL_S)):
        value = gpu.get(key, default)
        # bool is an int subclass; `vram_mb: true` is a typo, not one megabyte.
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise RunError(f"gpu.{key} must be a positive integer, got {value!r}")
        out[key] = value
    for key, default in (("host_pref", "auto"), ("backend", "")):
        value = gpu.get(key, default)
        if not isinstance(value, str):
            raise RunError(f"gpu.{key} must be a string, got {value!r}")
        out[key] = value or default
    return out


#: Ids are typed by humans ("awrun bump r-7f3a --priority 5"), so short and an
#: unambiguous alphabet — no 0/o/1/l. Same convention as decisions/store.py.
_ID_ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"
_ID_RE = re.compile(r"^r-[" + _ID_ALPHABET + r"]{4,12}$")


class RunError(RuntimeError):
    """A run-queue operation that could not be completed as asked."""


def runs_dir() -> Path:
    """The queue root, honouring AITHER_AWRUN_DIR for tests and tenants."""
    env = os.getenv("AITHER_AWRUN_DIR", "").strip()
    if env:
        return Path(env)
    return Path.home() / ".aither" / "awrun"


def _new_id() -> str:
    return "r-" + "".join(secrets.choice(_ID_ALPHABET) for _ in range(8))


@dataclass
class RunItem:
    id: str
    kind: str                       # "agent" | "ci"
    spec: dict[str, Any] = field(default_factory=dict)
    priority: int = 0               # higher = more urgent
    status: str = STATUS_QUEUED
    paths: list[str] = field(default_factory=list)
    claimed_by: Optional[str] = None
    result: Optional[dict[str, Any]] = None
    #: What this run needs from a GPU (see validate_gpu). None = not a GPU run.
    gpu: Optional[dict[str, Any]] = None
    #: Epoch seconds before which claim_next() will not hand this item out.
    #: Set by requeue(); 0 = claimable now.
    not_before: float = 0.0
    #: How many times this item went back to queued/ (backoff input).
    requeues: int = 0
    #: Why it last went back -- the lease door's own words (reason, lanes,
    #: card id), so `awrun queue` can say what it is waiting for.
    wait: Optional[dict[str, Any]] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "RunItem":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in known})


class RunStore:
    """Disk-backed run queue. Safe across processes; cheap enough to poll."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else runs_dir()
        for status in ALL_STATUSES:
            (self.path / status).mkdir(parents=True, exist_ok=True)

    # ── paths ────────────────────────────────────────────────────────────

    def _validate_id(self, item_id: str) -> None:
        if not _ID_RE.match(item_id or ""):
            # Ids reach this from a CLI argv and, later, an HTTP body — the
            # same "../../etc/passwd" concern decisions/store.py's _file()
            # exists for.
            raise RunError(f"not a valid run id: {item_id!r}")

    def _file_in(self, status: str, item_id: str) -> Path:
        self._validate_id(item_id)
        return self.path / status / f"{item_id}.json"

    def _locate(self, item_id: str) -> Optional[tuple[str, Path]]:
        """Find which status directory currently holds this id. A run moves
        directories over its life, so callers must not assume a status."""
        self._validate_id(item_id)
        for status in ALL_STATUSES:
            p = self.path / status / f"{item_id}.json"
            if p.exists():
                return status, p
        return None

    # ── writes ───────────────────────────────────────────────────────────

    def _write(self, status: str, item: RunItem) -> None:
        target = self._file_in(status, item.id)
        tmp = target.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(item.to_dict(), indent=2), encoding="utf-8")
        os.replace(tmp, target)

    def submit(self, kind: str, spec: dict[str, Any], *,
               priority: int = 0, paths: Optional[list[str]] = None,
               gpu: Optional[dict[str, Any]] = None) -> RunItem:
        if kind not in KINDS:
            raise RunError(f"kind must be one of {KINDS}, got {kind!r}")
        # `gpu=` wins; a submitter that can only send a spec (an HTTP body, a
        # CLI --spec-json) may carry it as spec["gpu"] instead. Validated
        # BEFORE an id is minted, so a refused submit leaves nothing on disk.
        if gpu is None and isinstance(spec, dict):
            gpu = spec.get("gpu")
        gpu = validate_gpu(kind, gpu)
        for _ in range(50):
            candidate = _new_id()
            if self._locate(candidate) is None:
                break
        else:
            raise RunError("could not mint an unused run id")
        item = RunItem(id=candidate, kind=kind, spec=dict(spec), priority=priority,
                        status=STATUS_QUEUED, paths=list(paths or []), gpu=gpu)
        self._write(STATUS_QUEUED, item)
        return item

    def bump(self, item_id: str, priority: int) -> RunItem:
        """Change priority in place — no directory move, so no claim race is
        possible here. Only legal on an OPEN item; bumping something already
        done/failed/cancelled is a no-op error, not a silent rewrite of
        history."""
        located = self._locate(item_id)
        if located is None:
            raise RunError(f"no such run: {item_id}")
        status, _ = located
        if status not in OPEN_STATUSES:
            raise RunError(f"run {item_id} is already {status}, cannot bump priority")
        item = self._read(status, item_id)
        if item is None:
            raise RunError(f"no such run: {item_id}")
        item.priority = priority
        item.updated_at = time.time()
        self._write(status, item)
        return item

    def _move(self, item_id: str, from_status: str, to_status: str, *,
               mutate=None) -> Optional[RunItem]:
        """The core cross-process-safe primitive: os.rename between status
        directories. Returns None (never raises) when the source is already
        gone — that is a lost race, not an error, and callers (claim_next in
        particular) rely on being able to try the next candidate."""
        src = self._file_in(from_status, item_id)
        dst = self._file_in(to_status, item_id)
        item = self._read(from_status, item_id)
        if item is None:
            return None
        if mutate is not None:
            mutate(item)
        item.status = to_status
        item.updated_at = time.time()
        # Write the NEW content at the OLD path first (still atomic, via
        # os.replace), then rename into the new directory. Two processes
        # racing on the same src both attempt this rename; exactly one
        # succeeds, the other's os.rename raises FileNotFoundError because
        # the source is gone by the time it gets there.
        tmp = src.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(item.to_dict(), indent=2), encoding="utf-8")
        try:
            os.replace(tmp, src)
            os.rename(src, dst)
        except FileNotFoundError:
            tmp.unlink(missing_ok=True)
            return None
        return item

    def claim(self, item_id: str, *, worker_id: str) -> Optional[RunItem]:
        """Attempt to claim a specific queued item. Returns None (not an
        exception) if someone else claimed it first — that is the expected,
        routine outcome of a race, not a failure."""
        def _set_claimant(item: RunItem) -> None:
            item.claimed_by = worker_id
        return self._move(item_id, STATUS_QUEUED, STATUS_CLAIMED, mutate=_set_claimant)

    def claim_next(self, *, worker_id: str, kind: Optional[str] = None,
                   skip: Optional[Callable[[RunItem], bool]] = None,
                   now: Optional[float] = None) -> Optional[RunItem]:
        """Claim the single highest-priority queued item, retrying the next
        candidate if a race loses the top one, or if `skip` says this
        particular item is not claimable right now (Phase 4: an item whose
        `paths` collide with a peer's live awgit lease). `skip` returning
        True does NOT remove the item from the queue -- it is tried again on
        the next dispatch cycle, once the lease has cleared. Returns None
        only when nothing claimable remains after both checks.

        An item whose `not_before` is still in the future (requeue() put it
        back with a backoff) is passed over the same way: it stays queued and
        a lower-priority item may run ahead of it meanwhile -- a run waiting
        on a busy GPU must not idle the whole queue. `now` is injectable for
        tests only."""
        at = time.time() if now is None else now
        for item in self.list(statuses=[STATUS_QUEUED], kind=kind):
            if item.not_before and item.not_before > at:
                continue
            if skip is not None and skip(item):
                continue
            claimed = self.claim(item.id, worker_id=worker_id)
            if claimed is not None:
                return claimed
        return None

    def start(self, item_id: str) -> Optional[RunItem]:
        return self._move(item_id, STATUS_CLAIMED, STATUS_RUNNING)

    def finish(self, item_id: str, *, status: str, result: Optional[dict[str, Any]] = None
               ) -> Optional[RunItem]:
        if status not in (STATUS_DONE, STATUS_FAILED):
            raise RunError(f"finish() status must be done or failed, got {status!r}")

        def _set_result(item: RunItem) -> None:
            item.result = result

        return self._move(item_id, STATUS_RUNNING, status, mutate=_set_result)

    def requeue(self, item_id: str, not_before: float, *,
                wait: Optional[dict[str, Any]] = None) -> Optional[RunItem]:
        """Put a CLAIMED or RUNNING item back in queued/, not claimable before
        `not_before` (epoch seconds). This is "not now", never "failed": the
        item keeps its id, priority and age, loses its claimant, and counts
        the trip in `requeues`. `wait` records why (a lease door's refusal).

        Returns None when the item is no longer claimed/running under this id
        -- cancelled meanwhile, or requeued by someone else -- which is a lost
        race, not an error. Requeueing a queued or closed item is an error:
        the first is a no-op that would silently reset a backoff, the second
        would rewrite history."""
        located = self._locate(item_id)
        if located is None:
            raise RunError(f"no such run: {item_id}")
        status, _ = located
        if status not in (STATUS_CLAIMED, STATUS_RUNNING):
            raise RunError(f"run {item_id} is {status}, only a claimed or running "
                           f"run can be requeued")
        try:
            when = float(not_before)
        except (TypeError, ValueError):
            raise RunError(f"not_before must be epoch seconds, got {not_before!r}") from None

        def _back(item: RunItem) -> None:
            item.claimed_by = None
            item.not_before = when
            item.requeues = int(item.requeues or 0) + 1
            item.wait = wait

        return self._move(item_id, status, STATUS_QUEUED, mutate=_back)

    def cancel(self, item_id: str) -> Optional[RunItem]:
        located = self._locate(item_id)
        if located is None:
            raise RunError(f"no such run: {item_id}")
        status, _ = located
        if status not in OPEN_STATUSES:
            return self._read(status, item_id)
        return self._move(item_id, status, STATUS_CANCELLED)

    # ── reads ────────────────────────────────────────────────────────────

    @staticmethod
    def _read_file(target: Path) -> Optional[RunItem]:
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # A file mid-write, or genuinely corrupt. Skipped, never fatal —
            # one bad file must not empty the whole queue.
            return None
        try:
            return RunItem.from_dict(raw)
        except TypeError:
            return None

    def _read(self, status: str, item_id: str) -> Optional[RunItem]:
        target = self._file_in(status, item_id)
        if not target.exists():
            return None
        return self._read_file(target)

    def get(self, item_id: str) -> Optional[RunItem]:
        located = self._locate(item_id)
        if located is None:
            return None
        _, target = located
        return self._read_file(target)

    def list(self, *, statuses: Optional[list[str]] = None,
              kind: Optional[str] = None) -> list[RunItem]:
        """Sorted by priority (highest first), then submission order
        (oldest first) — a FIFO among equal priorities, never arbitrary."""
        wanted = statuses if statuses is not None else list(ALL_STATUSES)
        items: list[RunItem] = []
        for status in wanted:
            d = self.path / status
            if not d.is_dir():
                continue
            for f in sorted(d.glob("*.json")):
                item = self._read_file(f)
                if item is None:
                    continue
                if kind is not None and item.kind != kind:
                    continue
                items.append(item)
        items.sort(key=lambda it: (-it.priority, it.created_at))
        return items


def get_store() -> RunStore:
    return RunStore()
