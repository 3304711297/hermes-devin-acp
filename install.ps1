[CmdletBinding()]
param(
    [string]$HermesHome = ""
)

$ErrorActionPreference = "Stop"

if (-not $HermesHome) {
    if ($env:HERMES_HOME) {
        $HermesHome = $env:HERMES_HOME
    } else {
        $HermesHome = Join-Path $HOME ".hermes"
    }
}

$target = Join-Path $HermesHome "plugins\model-providers\devin-acp"
$source = Join-Path $PSScriptRoot "plugins\model-providers\devin-acp"

if (-not (Test-Path $source)) {
    throw "Plugin source directory not found: $source"
}

New-Item -ItemType Directory -Force -Path $target | Out-Null
Copy-Item -Path (Join-Path $source "*") -Destination $target -Recurse -Force

Write-Host "Installed Hermes Devin ACP provider to: $target"
Write-Host "Restart Hermes, then run /model and select 'Devin Subscription'."
