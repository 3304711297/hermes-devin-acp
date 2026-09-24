from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from _plugin_loader import load_plugin  # noqa: E402


def session_with_models(values, current=None):
    """Minimal session/new result advertising exactly ``values`` for the model option."""
    return {
        "sessionId": "s1",
        "configOptions": [
            {
                "id": "model",
                "type": "select",
                "currentValue": current or (values[0] if values else ""),
                "options": [{"value": v, "name": v.upper()} for v in values],
            }
        ],
    }


class _CaptureClient:
    """Stands in for the ACP transport and records what the plugin actually sends."""

    def __init__(self):
        self._runtime = (None, None, None)
        self.calls = []

    def _request(self, process, inbox, stderr_tail, method, params, *, timeout_seconds):
        self.calls.append((method, params))
        return {}

    def sent_model_values(self):
        return [
            params["value"]
            for method, params in self.calls
            if method == "session/set_config_option" and params.get("configId") == "model"
        ]


def test_profile_contract():
    module, registered = load_plugin()
    assert registered and registered[0].name == "devin-acp"
    profile = module.devin_acp
    assert profile.auth_type == "external_process"
    assert profile.process_command == "devin"
    assert profile.process_args == ("acp",)
    assert "swe" in profile.fallback_models


def test_fallback_models_are_only_names_that_actually_work():
    """Only aliases proven to resolve on a real account may be offered up front."""
    module, _ = load_plugin()
    fallback = tuple(module.devin_acp.fallback_models)
    assert fallback == ("swe",)
    for broken in ("adaptive", "gpt", "opus", "sonnet"):
        assert broken not in fallback


def test_model_catalog_parser_json_shape():
    module, _ = load_plugin()
    raw = json.dumps(
        {
            "families": [
                {
                    "name": "SWE",
                    "models": [
                        {"id": "swe"},
                        {"id": "swe-2"},
                    ],
                },
                {"name": "Claude", "models": [{"id": "claude-opus-4-8-high"}]},
            ]
        }
    )
    assert module._parse_model_catalog(raw) == ["swe", "swe-2", "claude-opus-4-8-high"]


def test_session_model_ids_and_selection():
    module, _ = load_plugin()
    session = {
        "sessionId": "abc",
        "configOptions": [
            {
                "id": "model",
                "type": "select",
                "currentValue": "swe",
                "options": [
                    {"value": "swe", "name": "SWE"},
                    {"value": "swe-2", "name": "SWE-2"},
                ],
            }
        ],
    }
    assert module._session_model_ids(session) == ["swe", "swe-2"]


def test_session_model_ids_keep_the_advertised_order():
    module, _ = load_plugin()
    session = session_with_models(["swe-1-6-slow", "swe-2", "swe-1-5-fast"])
    assert module._session_model_ids(session) == ["swe-1-6-slow", "swe-2", "swe-1-5-fast"]


def test_resolve_model_choice_accepts_exact_advertised_value():
    module, _ = load_plugin()
    choice = module._resolve_model_choice("swe-1-6-slow", ["swe-1-6-slow", "gpt-5-codex"])
    assert choice.value == "swe-1-6-slow"
    assert choice.note == ""


def test_resolve_model_choice_normalizes_dotted_version_ids():
    module, _ = load_plugin()
    choice = module._resolve_model_choice("swe-1.6-slow", ["swe-1-6-slow"])
    assert choice.value == "swe-1-6-slow"
    assert choice.note == ""


def test_resolve_model_choice_maps_family_alias_to_the_advertised_model():
    module, _ = load_plugin()
    choice = module._resolve_model_choice("swe", ["swe-1-6-slow"])
    assert choice.value == "swe-1-6-slow"
    assert "swe" in choice.note and "swe-1-6-slow" in choice.note


def test_resolve_model_choice_matches_a_sibling_family_id_regardless_of_order():
    module, _ = load_plugin()
    advertised = ["claude-opus-4-8-high", "claude-sonnet-4-5"]
    assert module._resolve_model_choice("claude-sonnet-5", advertised).value == "claude-sonnet-4-5"
    reversed_advertised = list(reversed(advertised))
    assert module._resolve_model_choice("claude-sonnet-5", reversed_advertised).value == "claude-sonnet-4-5"


def test_resolve_model_choice_ties_break_on_session_order():
    """A family alias picks the model Devin itself advertises first."""
    module, _ = load_plugin()
    advertised = ["swe-2-enterprise", "swe-1-6-slow", "swe-1-5-fast"]
    assert module._resolve_model_choice("swe", advertised).value == "swe-2-enterprise"
    assert module._resolve_model_choice("swe", list(reversed(advertised))).value == "swe-1-5-fast"


def test_resolve_model_choice_falls_back_to_the_session_default():
    module, _ = load_plugin()
    choice = module._resolve_model_choice("adaptive", ["swe-1-6-slow"], default="swe-1-6-slow")
    assert choice.value == "swe-1-6-slow"
    assert "adaptive" in choice.note and "swe-1-6-slow" in choice.note


def test_apply_model_option_resolves_unadvertised_model_instead_of_raising(caplog):
    """Selecting a catalog model the session never advertised must degrade, not explode."""
    module, _ = load_plugin()
    session = session_with_models(["swe-1-6-slow"])
    client = _CaptureClient()
    with caplog.at_level("WARNING"):
        effective = module._apply_model_option(
            client, session, "gpt-5-codex", timeout_seconds=1.0
        )
    assert effective == "swe-1-6-slow"
    assert client.sent_model_values() == ["swe-1-6-slow"]
    assert any("gpt-5-codex" in record.message and "swe-1-6-slow" in record.message for record in caplog.records)


def test_apply_model_option_reports_the_effective_model_it_sent():
    module, _ = load_plugin()
    session = session_with_models(["swe-1-6-slow", "swe-2"], current="swe-1-6-slow")
    client = _CaptureClient()
    assert module._apply_model_option(client, session, "swe", timeout_seconds=1.0) == "swe-1-6-slow"
    assert client.sent_model_values() == ["swe-1-6-slow"]


def test_apply_model_option_is_stable_within_a_session():
    module, _ = load_plugin()
    session = session_with_models(["swe-2-enterprise", "swe-1-6-slow", "swe-1-5-fast"])
    results = {
        module._apply_model_option(_CaptureClient(), session, "swe", timeout_seconds=1.0)
        for _ in range(20)
    }
    assert results == {"swe-2-enterprise"}


def test_model_choice_does_not_depend_on_hash_seed():
    """Regression: set iteration order used to make the picked SWE model random."""
    probe = pathlib.Path(__file__).with_name("_model_choice_probe.py")
    outputs = set()
    for seed in ("0", "1", "2", "3", "4"):
        env = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONIOENCODING": "utf-8"}
        proc = subprocess.run(
            [sys.executable, str(probe)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=120,
        )
        assert proc.returncode == 0, proc.stderr
        outputs.add(proc.stdout.strip().splitlines()[-1])
    assert len(outputs) == 1, outputs
    payload = json.loads(outputs.pop())
    assert payload["effective"] == "swe-1-6-slow"
    assert payload["sent"] == ["swe-1-6-slow"]


def test_cli_model_flag_only_carries_verified_or_alias_names():
    module, _ = load_plugin()
    assert module._cli_model_flag("swe", None) == "swe"
    assert module._cli_model_flag("gpt-5-codex", None) == ""
    assert module._cli_model_flag("swe-1.6-slow", ["swe-1-6-slow"]) == "swe-1-6-slow"
    assert module._cli_model_flag("gpt-5-codex", ["swe-1-6-slow"]) == "swe-1-6-slow"


def test_request_returns_a_response_that_arrived_as_the_process_exited():
    """Regression: a response already in the inbox must not be discarded as a timeout.

    The agent may answer and then exit before the pump thread delivers the
    response; _request used to break out of its loop on process.poll() without
    draining the inbox, turning a good answer into a spurious TimeoutError.
    """
    import queue
    from collections import deque

    module, _ = load_plugin()

    class DeadProc:
        def __init__(self):
            self.stdin = self

        def write(self, s):
            pass

        def flush(self):
            pass

        def poll(self):
            return 0  # already exited

    client = module.DevinACPClient(command="unused", args=[])
    client._next_id = 0
    inbox: queue.Queue = queue.Queue()
    inbox.put({"jsonrpc": "2.0", "id": 1, "result": {"sessionId": "s-exit"}})
    result = client._request(
        DeadProc(),
        inbox,
        deque(maxlen=50),
        "session/new",
        {"cwd": "/tmp"},
        timeout_seconds=5,
    )
    assert result == {"sessionId": "s-exit"}


def test_apply_model_option_degrades_when_the_option_advertises_no_options(caplog):
    """A model option with zero options must degrade, not kill the turn."""
    module, _ = load_plugin()
    session = {
        "sessionId": "s",
        "configOptions": [
            {"id": "model", "type": "select", "currentValue": "", "options": []}
        ],
    }
    client = _CaptureClient()
    with caplog.at_level("WARNING"):
        assert module._apply_model_option(client, session, "swe", timeout_seconds=1.0) == ""
    assert client.sent_model_values() == []
    assert any("no options" in record.message for record in caplog.records)
