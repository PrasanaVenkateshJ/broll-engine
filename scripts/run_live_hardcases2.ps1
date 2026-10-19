# Live hard cases, part 2, against the Modal demo: hard01 (abstract + numbers), hard04 (sad story),
# hard05 (9:16 subject at the edge; keeps its own 16:9 + 9:16). Three jobs, one at a time.
# Asks for the API token (hidden input, never printed or saved). Results: testpack\live\hardcases_2026-10-19\
#   .\scripts\run_live_hardcases2.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

$api = "https://prasanavenkateshj--broll-studio.us-east.modal.run"
$t = Read-Host "Modal API token" -AsSecureString
$env:BROLL_API_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($t))

$s = Join-Path $env:TEMP "hc2_in"
Remove-Item $s -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory $s | Out-Null
foreach ($f in 'hard01_abstract_and_statistics.json', 'hard04_sensitive_news.json', 'hard05_crop_subject_at_edge_9x16.json') {
    Copy-Item "testpack\inputs\$f" $s
}

try {
    .venv\Scripts\python tools\batch_run.py $s --out testpack\live\hardcases_2026-10-19 --api $api --ratios 16:9 --keep-input-ratios --concurrency 1 --timeout 3600
} finally {
    Remove-Item Env:BROLL_API_TOKEN -ErrorAction SilentlyContinue
    Remove-Item $s -Recurse -Force -ErrorAction SilentlyContinue
}
