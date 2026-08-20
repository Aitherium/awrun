"""awrun — a priority-aware queue and dispatcher for agentic runs and ad-hoc CI builds."""

from awrun.store import (
    ALL_STATUSES,
    CLOSED_STATUSES,
    KINDS,
    OPEN_STATUSES,
    RunError,
    RunItem,
    RunStore,
    get_store,
    runs_dir,
)

__all__ = [
    "ALL_STATUSES",
    "CLOSED_STATUSES",
    "KINDS",
    "OPEN_STATUSES",
    "RunError",
    "RunItem",
    "RunStore",
    "get_store",
    "runs_dir",
]

__version__ = "0.1.0"
