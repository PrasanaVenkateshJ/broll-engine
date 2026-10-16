# Fallback live demo (no account needed): runs the API + worker on THIS PC and makes them reachable
# from the internet through a Cloudflare "quick tunnel" (a random https://<words>.trycloudflare.com URL).
# Use it when the Modal demo is unavailable. The URL lives only while this window stays open.
#
# Usage (project folder):  .\scripts\tunnel.ps1            or  .\scripts\tunnel.ps1 -Port 8001
# Stop with Ctrl+C: the tunnel, the API and the worker are all stopped.
#
# - cloudflared.exe is downloaded once into tools\bin\ (git-ignored) from Cloudflare's GitHub releases,
#   and its Windows code signature must be valid and from Cloudflare, or the script stops.
# - Your .env is used (real keys, real models), but the token "dev-token" or no token is refused:
#   the URL is public, so set a long random API_TOKENS value in .env first.
# - Same cost guards as the Modal demo (daily job cap, 5-minute audio limit, strict rate limits).
param([int]$Port = 8000)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)   # go to the project folder

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Error "No .venv found. Run: py -3.11 -m venv .venv ; .venv\Scripts\python -m pip install -r requirements.txt"
}

# ---------- 1) A real token must be set (the tunnel URL is public) ----------
$tokens = $env:API_TOKENS
if (-not $tokens -and (Test-Path ".env")) {
    $line = Select-String -Path ".env" -Pattern '^API_TOKENS=(.*)$' | Select-Object -First 1
    if ($line) { $tokens = $line.Matches[0].Groups[1].Value.Trim() }
}
$tokenList = @(($tokens -split ",") | ForEach-Object { $_.Trim() } | Where-Object { $_ })
if ($tokenList.Count -eq 0 -or ($tokenList | Where-Object { $_ -eq "dev-token" -or $_.Length -lt 16 })) {
    Write-Error "API_TOKENS in .env must hold only long random tokens (16+ characters, not 'dev-token') before going public."
}

# ---------- 2) cloudflared, downloaded on demand ----------
$bin = Join-Path (Get-Location) "tools\bin"
$exe = Join-Path $bin "cloudflared.exe"
if (-not (Test-Path $exe)) {
    New-Item -ItemType Directory -Force $bin | Out-Null
    $url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
    Write-Host "Downloading cloudflared from $url ..."
    Invoke-WebRequest -Uri $url -OutFile $exe -UseBasicParsing
}
$sig = Get-AuthenticodeSignature $exe
if ($sig.Status -ne "Valid" -or $sig.SignerCertificate.Subject -notmatch "Cloudflare") {
    Remove-Item $exe -Force
    Write-Error "cloudflared.exe has no valid Cloudflare signature (status $($sig.Status)); deleted it. Try again."
}
Write-Host "cloudflared: $(& $exe --version)"

# ---------- 3) Tunnel first (to learn the public URL), then the API + worker ----------
$logs = Join-Path (Get-Location) "data\tunnel"
New-Item -ItemType Directory -Force $logs | Out-Null
$tunnelLog = Join-Path $logs "cloudflared.log"
Remove-Item $tunnelLog -ErrorAction SilentlyContinue
$procs = @()
try {
    $procs += Start-Process -FilePath $exe -PassThru -NoNewWindow -RedirectStandardError $tunnelLog `
        -RedirectStandardOutput (Join-Path $logs "cloudflared.out.log") `
        -ArgumentList "tunnel", "--no-autoupdate", "--url", "http://127.0.0.1:$Port"
    $publicUrl = $null
    for ($i = 0; $i -lt 60 -and -not $publicUrl; $i++) {
        Start-Sleep -Seconds 1
        if (Test-Path $tunnelLog) {
            $m = Select-String -Path $tunnelLog -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com' | Select-Object -First 1
            if ($m) { $publicUrl = $m.Matches[0].Value }
        }
    }
    if (-not $publicUrl) { throw "The tunnel did not report a URL within 60 s; see $tunnelLog" }

    # Settings for this run only (environment variables win over .env).
    $env:PUBLIC_BASE_URL = $publicUrl          # https links in job answers and callbacks
    $env:ALLOW_PRIVATE_URLS = "false"
    $env:DEMO_MAX_JOBS_PER_DAY = "40"
    $env:MAX_AUDIO_SECONDS = "300"
    $env:RATE_LIMIT_POST_PER_MINUTE = "4"
    $env:RATE_LIMIT_GET_PER_MINUTE = "300"
    $env:WORKER_CONCURRENCY = "1"

    $procs += Start-Process -FilePath ".venv\Scripts\python.exe" -PassThru -NoNewWindow `
        -RedirectStandardError (Join-Path $logs "api.log") -RedirectStandardOutput (Join-Path $logs "api.out.log") `
        -ArgumentList "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "$Port"
    $procs += Start-Process -FilePath ".venv\Scripts\python.exe" -PassThru -NoNewWindow `
        -RedirectStandardError (Join-Path $logs "worker.log") -RedirectStandardOutput (Join-Path $logs "worker.out.log") `
        -ArgumentList "-m", "app.worker"

    $ready = $false
    for ($i = 0; $i -lt 60 -and -not $ready; $i++) {
        Start-Sleep -Seconds 1
        try { $ready = (Invoke-WebRequest "http://127.0.0.1:$Port/ready" -UseBasicParsing -TimeoutSec 5).StatusCode -eq 200 } catch { }
    }
    if (-not $ready) { throw "The API did not become ready within 60 s; see $logs\api.log" }

    Write-Host ""
    Write-Host "===== PUBLIC URL: $publicUrl =====" -ForegroundColor Green
    Write-Host "Studio: $publicUrl/   (paste your API token in the top bar)"
    Write-Host "Check:  .venv\Scripts\python tools\live_check.py --url $publicUrl"
    Write-Host "Logs:   $logs    Stop: Ctrl+C"
    while ($true) {
        Start-Sleep -Seconds 2
        $dead = $procs | Where-Object { $_.HasExited }
        if ($dead) { throw "A process stopped (see the logs in $logs)." }
    }
}
finally {
    foreach ($p in $procs) { if ($p -and -not $p.HasExited) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue } }
    Remove-Item Env:PUBLIC_BASE_URL, Env:DEMO_MAX_JOBS_PER_DAY, Env:MAX_AUDIO_SECONDS, Env:RATE_LIMIT_POST_PER_MINUTE, `
        Env:RATE_LIMIT_GET_PER_MINUTE, Env:WORKER_CONCURRENCY, Env:ALLOW_PRIVATE_URLS -ErrorAction SilentlyContinue
    Write-Host "Stopped the tunnel, the API and the worker."
}
