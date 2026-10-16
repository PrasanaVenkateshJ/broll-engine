# Run ALL tests (unit + integration + e2e) with the local .venv. Mock adapters only: no API keys,
# no models, no Docker. (The one MinIO test is skipped here; it runs in Docker / CI.)
# Usage:  .\scripts\test.ps1                 all tests
#         .\scripts\test.ps1 tests\unit      only one folder
#         .\scripts\test.ps1 -k swap         any pytest option works
# Docker equivalent:  docker compose run --rm api pytest
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)   # go to the project folder

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Error "No .venv found. Run: py -3.11 -m venv .venv ; .venv\Scripts\python -m pip install -r requirements.txt"
}

& .venv\Scripts\python -m pytest @args
exit $LASTEXITCODE
