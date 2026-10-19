# Live hard-case battery against the Modal demo (about 11 jobs, estimated cost ~ $0.18 of Modal credit).
# Asks for the API token (hidden input, never printed or saved). Results: testpack\live\hardcases_2026-10-18\pair and \rest
#   .\scripts\run_live_hardcases.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

$api = "https://prasanavenkateshj--broll-studio.us-east.modal.run"
$t = Read-Host "Modal API token" -AsSecureString
$env:BROLL_API_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($t))

$a = Join-Path $env:TEMP "hc_pair"; $b = Join-Path $env:TEMP "hc_rest"
Remove-Item $a, $b -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory $a, $b | Out-Null
Copy-Item testpack\live\hardcases_inputs\*.json $a   # one pair of simultaneous jobs
foreach ($f in 'hard09_ten_minute.m4a', 'hard07_silent_audio.wav', 'hard08_music_only_chords.wav',
               'hard09_wrong_type_text_renamed.mp3', 'hard10_empty_script.txt',
               'hard02_named_person_and_brand.json', 'hard03_double_meanings.json') {
    Copy-Item "testpack\inputs\$f" $b
}

try {
    .venv\Scripts\python tools\batch_run.py $a --out testpack\live\hardcases_2026-10-18\pair --api $api --ratios 16:9 --concurrency 2 --timeout 3600
    .venv\Scripts\python tools\batch_run.py $b --out testpack\live\hardcases_2026-10-18\rest --api $api --ratios 16:9 --concurrency 1 --timeout 14400
} finally {
    Remove-Item Env:BROLL_API_TOKEN -ErrorAction SilentlyContinue
    Remove-Item $a, $b -Recurse -Force -ErrorAction SilentlyContinue
}
