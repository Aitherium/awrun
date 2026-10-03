"""The host plugin's GPU lease client reaches the dispatcher.

awrun's stdlib lease client holds no credentials, so a host door answered every
acquire with 401 (measured 2026-10-03). The host plugin file can now expose
`gpu_lease_client()`; the CLI registers it beside the capacity hooks, and the
dispatcher's `main()` runs the same registration.
"""
from __future__ import annotations

import textwrap

from awrun import cli, plugins
from awrun.dispatcher import _lease_client

PLUGIN = textwrap.dedent('''
    import types
    CLIENT = types.SimpleNamespace(name="host-client")
    def provision_capacity(*a, **k): return {}
    def reap_capacity(*a, **k): return {}
    def gpu_lease_client(): return CLIENT
''')


def test_host_plugin_lease_client_is_what_the_dispatcher_uses(tmp_path, monkeypatch):
    path = tmp_path / "host_plugin.py"
    path.write_text(PLUGIN, encoding="utf-8")
    monkeypatch.setenv("AWRUN_CAPACITY_PLUGIN", str(path))
    plugins.clear()
    try:
        cli._register_capacity_provider()
        assert plugins.PROVISION_IMPORT_ERROR is None
        assert getattr(_lease_client(), "name", None) == "host-client"
    finally:
        plugins.clear()


def test_without_the_hook_the_builtin_client_is_used(tmp_path, monkeypatch):
    path = tmp_path / "host_plugin.py"
    path.write_text(PLUGIN.replace("def gpu_lease_client", "def _not_exported"),
                    encoding="utf-8")
    monkeypatch.setenv("AWRUN_CAPACITY_PLUGIN", str(path))
    plugins.clear()
    try:
        cli._register_capacity_provider()
        from awrun import gpu_lease
        assert _lease_client() is gpu_lease
    finally:
        plugins.clear()
