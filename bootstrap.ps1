# One-shot setup: uv environment + pinned portable toolchain + VapourSynth plugins.
# Usage:  powershell -ExecutionPolicy Bypass -File bootstrap.ps1 [-SkipBench]
param([switch]$SkipBench)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Error 'uv is required: https://docs.astral.sh/uv/getting-started/installation/'
}
git submodule update --init --recursive
uv sync
if (-not (Test-Path config.local.toml)) { Copy-Item config.example.toml config.local.toml }

$vcookArgs = @('bootstrap')
if ($SkipBench) { $vcookArgs += '--skip-bench' }
uv run vcook @vcookArgs
exit $LASTEXITCODE
