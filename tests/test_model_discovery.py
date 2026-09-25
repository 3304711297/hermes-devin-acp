"""Discovery-path tests: what `list_models()` / `fetch_models()` expose to Hermes.

These drive the real plugin through the fake devin CLI in tests/fake_devin_acp.py,
which doubles as a `devin acp` server and as a `devin models list` command.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from _plugin_loader import FAKE, load_plugin  # noqa: E402


@pytest.fixture()
def plugin():
    module, _ = load_plugin()
    return module


@pytest.fixture()
def fake_credentials(plugin, monkeypatch):
    """Point the profile's credential seam at the fake CLI (never the real devin)."""
    monkeypatch.setattr(
        plugin,
        "_resolve_external_credentials",
        lambda profile: {
            "api_key": profile.name,
            "base_url": profile.base_url,
            "command": sys.executable,
            "args": [str(FAKE)],
        },
    )
    return plugin


def make_client(module, tmp_path, monkeypatch, env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("HERMES_DEVIN_ACP_MODE", raising=False)
    return module.DevinACPClient(
        command=sys.executable, args=[str(FAKE)], acp_cwd=str(tmp_path)
    )


def test_list_models_only_exposes_values_the_session_accepts(plugin, tmp_path, monkeypatch):
    """A rich CLI catalog must not leak into the picker when the session is narrower."""
    client = make_client(
        plugin,
        tmp_path,
        monkeypatch,
        {
            "FAKE_DEVIN_MODEL_OPTIONS": "swe-1-6-slow",
            "FAKE_DEVIN_CATALOG": "swe-1-6-slow,gpt-5-codex,claude-opus-4-8-high,swe-2",
        },
    )
    assert client.list_models(timeout_seconds=5) == ["swe-1-6-slow"]


def test_list_models_falls_back_to_the_cli_catalog_when_the_session_advertises_nothing(
    plugin, tmp_path, monkeypatch
):
    client = make_client(
        plugin,
        tmp_path,
        monkeypatch,
        {
            "FAKE_DEVIN_MODEL_OPTIONS": "",
            "FAKE_DEVIN_CATALOG": "swe,gpt-5-codex",
        },
    )
    assert client.list_models(timeout_seconds=5) == ["swe", "gpt-5-codex"]


def test_list_models_falls_back_to_the_cli_catalog_when_the_session_probe_fails(
    plugin, tmp_path, monkeypatch
):
    client = make_client(
        plugin,
        tmp_path,
        monkeypatch,
        {
            "FAKE_DEVIN_MODEL_OPTIONS": "swe-1-6-slow",
            "FAKE_DEVIN_CATALOG": "swe-1-6-slow,gpt-5-codex",
            "FAKE_DEVIN_FAIL_SESSION": "1",
        },
    )
    assert client.list_models(timeout_seconds=5) == ["swe-1-6-slow", "gpt-5-codex"]


def test_fetch_models_exposes_the_session_verified_set(fake_credentials, monkeypatch):
    monkeypatch.setenv("FAKE_DEVIN_MODEL_OPTIONS", "swe-1-6-slow")
    monkeypatch.setenv(
        "FAKE_DEVIN_CATALOG", "swe-1-6-slow,gpt-5-codex,claude-opus-4-8-high"
    )
    models = fake_credentials.devin_acp.fetch_models(timeout=5)
    assert models == ["swe-1-6-slow"]
    assert "gpt-5-codex" not in (models or [])


def test_every_exposed_model_can_actually_be_selected(plugin, fake_credentials, tmp_path, monkeypatch):
    """The picker contract: anything fetch_models() exposes must be settable verbatim."""
    monkeypatch.setenv("FAKE_DEVIN_MODEL_OPTIONS", "swe-1-6-slow,swe-2")
    monkeypatch.setenv("FAKE_DEVIN_CATALOG", "swe-1-6-slow,swe-2,gpt-5-codex")
    models = plugin.devin_acp.fetch_models(timeout=5) or []
    assert models
    client = plugin.DevinACPClient(command=sys.executable, args=[str(FAKE)], acp_cwd=str(tmp_path))
    for model in models:
        session = client._new_session(timeout_seconds=5, allow_terminal=False)
        assert plugin._apply_model_option(client, session, model, timeout_seconds=5) == model
    client.close()


RICH_CATALOG = [
    "swe-1-6-slow",
    "swe-2",
    "swe-1-5-fast",
    "gpt-5-codex",
    "claude-opus-4-8-high",
    "claude-sonnet-4-5",
    "gemini-2-5-pro",
    "deepseek-v3",
    "kimi-k2",
    "glm-4-6",
    *[f"gemini-2-5-flash-{i}" for i in range(34)],
]


def test_single_model_account_is_reported_honestly_and_never_raises(
    plugin, fake_credentials, tmp_path, monkeypatch
):
    """Real-account shape: 44 catalog models, but the session advertises only swe-1-6-slow.

    Before the fix the picker offered all 44 and every non-SWE choice raised
    RuntimeError. Now the picker offers the one selectable model and any other
    choice degrades to it instead of exploding.
    """
    assert len(RICH_CATALOG) == 44
    monkeypatch.setenv("FAKE_DEVIN_MODEL_OPTIONS", "swe-1-6-slow")
    monkeypatch.setenv("FAKE_DEVIN_CATALOG", ",".join(RICH_CATALOG))

    assert plugin.devin_acp.fetch_models(timeout=5) == ["swe-1-6-slow"]

    client = plugin.DevinACPClient(command=sys.executable, args=[str(FAKE)], acp_cwd=str(tmp_path))
    session = client._new_session(timeout_seconds=5, allow_terminal=False)
    for unwanted in ("gpt-5-codex", "claude-opus-4-8-high", "adaptive", "opus", "sonnet", "gpt"):
        assert plugin._apply_model_option(client, session, unwanted, timeout_seconds=5) == "swe-1-6-slow"
    client.close()


def test_a_narrower_session_recovers_the_full_catalog_when_it_advertises_nothing(
    plugin, fake_credentials, tmp_path, monkeypatch
):
    """If the session has no model option at all, the CLI catalog is still offered."""
    monkeypatch.setenv("FAKE_DEVIN_MODEL_OPTIONS", "")
    monkeypatch.setenv("FAKE_DEVIN_CATALOG", ",".join(RICH_CATALOG))
    models = plugin.devin_acp.fetch_models(timeout=5)
    assert models == RICH_CATALOG


def test_run_prompt_closes_the_acp_process_when_the_turn_times_out(
    plugin, tmp_path, monkeypatch
):
    """Regression: a timed-out turn must not leak the `devin acp` subprocess."""
    monkeypatch.delenv("HERMES_DEVIN_ACP_MODE", raising=False)
    fake = tmp_path / "fake_hang.py"
    fake.write_text(
        "import json, sys, time\n"
        "for raw in sys.stdin:\n"
        "    req = json.loads(raw)\n"
        "    method, rid = req.get('method'), req.get('id')\n"
        "    if method == 'initialize':\n"
        "        print(json.dumps({'jsonrpc':'2.0','id':rid,'result':{}}), flush=True)\n"
        "    elif method == 'session/new':\n"
        "        print(json.dumps({'jsonrpc':'2.0','id':rid,'result':"
        "{'sessionId':'s','configOptions':[]}}), flush=True)\n"
        "    elif method == 'session/prompt':\n"
        "        time.sleep(30)\n",
        encoding="utf-8",
    )
    client = plugin.DevinACPClient(
        command=sys.executable, args=[str(fake)], acp_cwd=str(tmp_path)
    )
    with pytest.raises(TimeoutError):
        client._run_prompt("hi", timeout_seconds=1, model="")
    assert client._proc is None
    assert client.is_closed


def test_close_terminates_terminals_the_agent_never_released(
    plugin, tmp_path, monkeypatch
):
    """Regression: terminals created by the agent must not outlive a failed turn."""
    monkeypatch.delenv("HERMES_DEVIN_ACP_MODE", raising=False)
    fake = tmp_path / "fake_orphan_term.py"
    fake.write_text(
        "import json, sys, time\n"
        "for raw in sys.stdin:\n"
        "    req = json.loads(raw)\n"
        "    method, rid = req.get('method'), req.get('id')\n"
        "    if method == 'initialize':\n"
        "        print(json.dumps({'jsonrpc':'2.0','id':rid,'result':{}}), flush=True)\n"
        "    elif method == 'session/new':\n"
        "        print(json.dumps({'jsonrpc':'2.0','id':rid,'result':"
        "{'sessionId':'s','configOptions':[]}}), flush=True)\n"
        "    elif method == 'session/prompt':\n"
        "        print(json.dumps({'jsonrpc':'2.0','id':77,'method':'terminal/create',"
        "'params':{'sessionId':'s','command':sys.executable,"
        "'args':['-c','import time; time.sleep(30)']}}), flush=True)\n"
        "        reply = json.loads(sys.stdin.readline())\n"
        "        assert 'terminalId' in reply['result'], reply\n"
        "        time.sleep(30)\n",
        encoding="utf-8",
    )
    states = []
    orig_create = plugin._terminal_create

    def spy(params, cwd):
        result = orig_create(params, cwd)
        states.append(plugin._TERMINALS[result["terminalId"]])
        return result

    monkeypatch.setattr(plugin, "_terminal_create", spy)
    before = set(plugin._TERMINALS)
    client = plugin.DevinACPClient(
        command=sys.executable, args=[str(fake)], acp_cwd=str(tmp_path)
    )
    with pytest.raises(TimeoutError):
        client._run_prompt("hi", timeout_seconds=3, model="")
    assert states, "the fake agent never created a terminal"
    assert all(state.process.poll() is not None for state in states)
    assert set(plugin._TERMINALS) == before
