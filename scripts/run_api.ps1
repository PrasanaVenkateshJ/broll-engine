# Start the API on http://localhost:8000 using the local .venv (no Docker).
# Usage:  .\scripts\run_api.ps1            or  .\scripts\run_api.ps1 -Port 8001
param([int]$Port = 8000)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)   # go to the project folder

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Error "No .venv found. Run: py -3.11 -m venv .venv ; .venv\Scripts\python -m pip install -r requirements.txt"
}
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env from .env.example (local storage, token 'dev-token')."
}

& .venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port $Port
