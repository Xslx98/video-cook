# One-shot setup: uv environment (VapourSynth R80 + plugin wheels) + portable toolchain + AI models.
# Usage:  powershell -ExecutionPolicy Bypass -File bootstrap.ps1 [-SkipBench]
param([switch]$SkipBench)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Error 'uv is required: https://docs.astral.sh/uv/getting-started/installation/'
}
git submodule update --init --recursive

# NVIDIA machines get the CUDA/TensorRT plugin set (BM3D, NLMeans, DPIR on the GPU).
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    Write-Host 'NVIDIA GPU detected: installing the nvidia extra'
    uv sync --extra nvidia
} else {
    uv sync
}
if (-not (Test-Path config.local.toml)) { Copy-Item config.example.toml config.local.toml }

$vcookArgs = @('bootstrap')
if ($SkipBench) { $vcookArgs += '--skip-bench' }
uv run vcook @vcookArgs
exit $LASTEXITCODE
