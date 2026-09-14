"""Test-suite guards.

Importing `_plugin_loader` here guarantees the fake `providers` / `hermes_cli` /
`tools` packages are installed for every test session, so no test can resolve
real Devin credentials or spawn a real `devin` CLI (which would burn the user's
account quota and make results machine-dependent).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _plugin_loader import FAKE, PLUGIN, load_plugin  # noqa: E402,F401

assert FAKE.exists(), f"fake Devin CLI missing: {FAKE}"
