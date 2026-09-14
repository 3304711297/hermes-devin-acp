from __future__ import annotations
import importlib.util, os, pathlib, stat, sys, tempfile, types

ROOT = pathlib.Path(__file__).resolve().parents[1]
PLUGIN = ROOT / 'plugins/model-providers/devin-acp/__init__.py'
FAKE = ROOT / 'tests/fake_devin_acp.py'

providers = types.ModuleType('providers')
class ProviderProfile:
    def __init__(self, **kwargs): self.__dict__.update(kwargs)
providers.base = types.ModuleType('providers.base')
providers.base.ProviderProfile = ProviderProfile
providers.register_provider = lambda p: None
sys.modules['providers'] = providers
sys.modules['providers.base'] = providers.base

hermes_cli = types.ModuleType('hermes_cli')
hermes_cli_compat = types.ModuleType('hermes_cli._subprocess_compat')
hermes_cli_compat.windows_hide_flags = lambda: 0
hermes_cli.auth = types.ModuleType('hermes_cli.auth')
hermes_cli.auth.resolve_external_process_provider_credentials = lambda name: {
    'api_key': name, 'base_url': 'acp://devin', 'command': sys.executable, 'args': [str(FAKE)]
}
sys.modules['hermes_cli'] = hermes_cli
sys.modules['hermes_cli._subprocess_compat'] = hermes_cli_compat
sys.modules['hermes_cli.auth'] = hermes_cli.auth

tools = types.ModuleType('tools')
tools.environments = types.ModuleType('tools.environments')
tools.environments.local = types.ModuleType('tools.environments.local')
tools.environments.local.hermes_subprocess_env = lambda inherit_credentials=True: dict(__import__('os').environ)
sys.modules['tools'] = tools
sys.modules['tools.environments'] = tools.environments
sys.modules['tools.environments.local'] = tools.environments.local

spec = importlib.util.spec_from_file_location('devin_plugin', PLUGIN)
mod = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(mod)

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
    os.environ['HERMES_DEVIN_ACP_MODE'] = 'bypass'
    c = mod.DevinACPClient(command=sys.executable, args=[str(FAKE)], acp_cwd=td)
    out, reasoning, actual = c._run_prompt('say hello', timeout_seconds=5, model='swe-2')
    assert out == 'hello from SWE-2', out
    assert reasoning == 'thinking', reasoning
    assert actual == 'swe-2', actual
    assert not c.is_closed or c._proc is None
    c.close()
    os.environ.pop('HERMES_DEVIN_ACP_MODE', None)

print('integration OK')
