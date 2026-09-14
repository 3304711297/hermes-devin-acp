# Hermes Devin ACP Provider

A current-Hermes model-provider plugin for the official Devin CLI over ACP.

It is intentionally **not** the older `raffr85/hermes-devin-acp` implementation. This version targets Hermes' current `ProviderProfile` external-process seam and keeps the Devin transport in the plugin itself.

## What it does

- Launches `devin acp` as a subprocess.
- Uses your already-authenticated Devin CLI account.
- Discovers account-available models from `devin models list --format json`, with ACP `session/new` model options as a fallback.
- Passes the selected Hermes model to Devin.
- Supports Devin ACP filesystem and terminal requests inside the Hermes workspace.
- Uses a Windsurf-compatible ACP client identity, configurable with `HERMES_DEVIN_ACP_WINDSURF_VERSION`.
- Keeps the provider out of Hermes core.

## Windows install

1. Install and authenticate Devin CLI:

```powershell
devin --version
devin auth login
devin models list
```

2. Copy this plugin directory to:

```text
%USERPROFILE%\.hermes\plugins\model-providers\devin-acp
```

or to `%HERMES_HOME%\plugins\model-providers\devin-acp` when `HERMES_HOME` is set.

3. Restart Hermes.

4. Open `/model` and look for **Devin Subscription**.

5. Choose `swe` or the exact SWE model exposed by your account.

## Optional environment overrides

```powershell
$env:HERMES_DEVIN_ACP_COMMAND = "C:\path\to\devin.exe"
$env:HERMES_DEVIN_ACP_ARGS = "acp"
$env:HERMES_DEVIN_ACP_WINDSURF_VERSION = "1.110.1"
```

## Important permission behavior

The provider does **not** silently auto-approve ACP permission requests. If Devin asks Hermes to approve a permission request that cannot be represented by the current Hermes ACP host, the request is cancelled.

The provider does implement the ACP terminal methods (`terminal/create`, `terminal/output`, `terminal/wait_for_exit`, `terminal/kill`, `terminal/release`) so normal Devin coding tasks can execute in the workspace when the Devin session mode permits them.

## Why `swe`

Devin's public CLI documentation says short names such as `swe` resolve to the latest version in the model family. This makes `swe` preferable to hard-coding a moving SWE model id.
