from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "model-providers" / "devin-acp" / "__init__.py"


def load_plugin():
    providers = types.ModuleType("providers")

    class ProviderProfile:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    base = types.ModuleType("providers.base")
    base.ProviderProfile = ProviderProfile
    registered = []

    def register_provider(profile):
        registered.append(profile)

    providers.register_provider = register_provider
    providers.base = base
    sys.modules["providers"] = providers
    sys.modules["providers.base"] = base

    spec = importlib.util.spec_from_file_location("devin_acp_test_plugin", PLUGIN)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module, registered


def test_profile_contract():
    module, registered = load_plugin()
    assert registered and registered[0].name == "devin-acp"
    profile = module.devin_acp
    assert profile.auth_type == "external_process"
    assert profile.process_command == "devin"
    assert profile.process_args == ("acp",)
    assert "swe" in profile.fallback_models


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
