"""`python -m awrun` — a shim-independent way in.

ATI007 (check_agent_tooling_installed.py, added 2026-08-21): a blocked
console shim leaves NO way to invoke a tool that lacks `__main__.py`. Smart
App Control judges each unsigned binary on its own reputation, so the next
blocked exe is a coin flip — the fallback has to exist BEFORE the outage.
"""

from awrun.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
