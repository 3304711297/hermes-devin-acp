"""Print the model the plugin would really select, for one PYTHONHASHSEED.

Run as a script (``python tests/_model_choice_probe.py``) so the parent test can
vary PYTHONHASHSEED, which is what made set-iteration order non-deterministic.
"""

from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from _plugin_loader import load_plugin  # noqa: E402

ADVERTISED = ["swe-1-6-slow", "swe-1-5-fast", "swe-2"]


class CaptureClient:
    def __init__(self):
        self._runtime = (None, None, None)
        self.sent = []

    def _request(self, process, inbox, stderr_tail, method, params, *, timeout_seconds):
        if method == "session/set_config_option":
            self.sent.append(params["value"])
        return {}


def main() -> int:
    module, _ = load_plugin()
    session = {
        "sessionId": "probe",
        "configOptions": [
            {
                "id": "model",
                "type": "select",
                "currentValue": ADVERTISED[0],
                "options": [{"value": v, "name": v.upper()} for v in ADVERTISED],
            }
        ],
    }
    client = CaptureClient()
    effective = module._apply_model_option(client, session, "swe", timeout_seconds=1.0)
    print(json.dumps({"effective": effective, "sent": client.sent}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
