"""Shared plugin loader for the test suite.

Importing this module installs stub ``providers`` / ``hermes_cli`` / ``tools``
packages *before* the plugin is imported, so:

* the plugin is exercised as it really ships (a standalone file), and
* ``fetch_models()`` can never resolve real credentials and reach a real
  ``devin`` CLI — the fake in tests/fake_devin_acp.py is the only backend.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "model-providers" / "devin-acp" / "__init__.py"
FAKE = ROOT / "tests" / "fake_devin_acp.py"

_CACHED: tuple[types.ModuleType, list] | None = None


def install_stubs() -> None:
    """Register the fake `providers`, `hermes_cli` and `tools` packages."""
    if "providers" in sys.modules and getattr(sys.modules["providers"], "_is_test_stub", False):
        return

    providers = types.ModuleType("providers")

    class ProviderProfile:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    base = types.ModuleType("providers.base")
    base.ProviderProfile = ProviderProfile
    registered: list = []
    providers.register_provider = registered.append
    providers.base = base
    providers._registered = registered
    providers._is_test_stub = True
    sys.modules["providers"] = providers
    sys.modules["providers.base"] = base

    hermes_cli = types.ModuleType("hermes_cli")
    compat = types.ModuleType("hermes_cli._subprocess_compat")
    compat.windows_hide_flags = lambda: 0
    auth = types.ModuleType("hermes_cli.auth")
    auth.resolve_external_process_provider_credentials = lambda name: {
        "api_key": name,
        "base_url": "acp://devin",
        "command": sys.executable,
        "args": [str(FAKE)],
    }
    hermes_cli.auth = auth
    hermes_cli._subprocess_compat = compat
    sys.modules["hermes_cli"] = hermes_cli
    sys.modules["hermes_cli.auth"] = auth
    sys.modules["hermes_cli._subprocess_compat"] = compat

    tools = types.ModuleType("tools")
    environments = types.ModuleType("tools.environments")
    local = types.ModuleType("tools.environments.local")
    local.hermes_subprocess_env = lambda inherit_credentials=True: dict(__import__("os").environ)
    environments.local = local
    tools.environments = environments
    sys.modules["tools"] = tools
    sys.modules["tools.environments"] = environments
    sys.modules["tools.environments.local"] = local


def load_plugin():
    """Import the plugin against the stubs; cached so tests share one module."""
    global _CACHED
    if _CACHED is not None:
        return _CACHED
    install_stubs()
    spec = importlib.util.spec_from_file_location("devin_acp_test_plugin", PLUGIN)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    _CACHED = (module, sys.modules["providers"]._registered)
    return _CACHED


install_stubs()
