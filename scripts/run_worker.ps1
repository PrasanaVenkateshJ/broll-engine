# Start the worker (picks up queued jobs) using the local .venv (no Docker).
# Run it in a SECOND PowerShell window, next to run_api.ps1. Stop with Ctrl+C.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)   # go to the project folder

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Error "No .venv found. Run: py -3.11 -m venv .venv ; .venv\Scripts\python -m pip install -r requirements.txt"
}
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env from .env.example (local storage, token 'dev-token')."
}

& .venv\Scripts\python -m app.worker
