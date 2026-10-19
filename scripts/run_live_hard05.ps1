# Live check of the 9:16 crop fix (c6ee5af) on the Modal demo: runs ONLY hard05 (16:9 + 9:16), one job.
# Asks for the API token (hidden input, never printed or saved). Results: testpack\live\hardcases_2026-10-19_cropfix\
#   .\scripts\run_live_hard05.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

$api = "https://prasanavenkateshj--broll-studio.us-east.modal.run"
$t = Read-Host "Modal API token" -AsSecureString
$env:BROLL_API_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($t))

$s = Join-Path $env:TEMP "hc_hard05_in"
Remove-Item $s -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory $s | Out-Null
Copy-Item testpack\inputs\hard05_crop_subject_at_edge_9x16.json $s

try {
    .venv\Scripts\python tools\batch_run.py $s --out testpack\live\hardcases_2026-10-19_cropfix --api $api --ratios 16:9,9:16 --concurrency 1 --timeout 3600
} finally {
    Remove-Item Env:BROLL_API_TOKEN -ErrorAction SilentlyContinue
    Remove-Item $s -Recurse -Force -ErrorAction SilentlyContinue
}
