"""End-to-end integration test against tests/fake_devin_acp.py.

Runs the real DevinACPClient through a full ACP turn (initialize -> session/new
-> set_config_option -> session/prompt -> terminal round-trip) without ever
touching a real `devin` CLI. Run with:  python tests/run_integration.py
"""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from _plugin_loader import FAKE, load_plugin  # noqa: E402

mod, _ = load_plugin()

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

# Same full turn for a model the session does not advertise verbatim: the plugin
# must degrade to the advertised one instead of raising.
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
    os.environ['HERMES_DEVIN_ACP_MODE'] = 'bypass'
    os.environ['FAKE_DEVIN_MODEL_OPTIONS'] = 'swe-2'
    c = mod.DevinACPClient(command=sys.executable, args=[str(FAKE)], acp_cwd=td)
    out, reasoning, actual = c._run_prompt('say hello', timeout_seconds=5, model='gpt-5-codex')
    assert actual == 'swe-2', actual
    c.close()
    os.environ.pop('HERMES_DEVIN_ACP_MODE', None)
    os.environ.pop('FAKE_DEVIN_MODEL_OPTIONS', None)

print('integration OK')
