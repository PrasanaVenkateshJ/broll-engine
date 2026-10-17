# Sample the memory (working set = RAM in use) of the API, the worker and every ffmpeg process,
# once per -Interval seconds, into a CSV file, until stopped (Ctrl+C) or -Seconds have passed.
# Used for the production numbers in testpack/metrics.md (peak memory of API + worker).
#
#   powershell -File tools\sample_memory.ps1 -Out testpack\logs\memory_run1.csv
#
# Columns: time (UTC), api_mb, worker_mb, ffmpeg_mb, ffmpeg_count, total_mb.
# A 1-second sample can miss a very short spike; the Windows peak working set of each Python process
# (PeakWorkingSet64, exact) is printed when the sampler stops, and tools\process_peaks.ps1 can read it anytime.
param(
    [Parameter(Mandatory = $true)][string]$Out,
    [double]$Interval = 1.0,
    [int]$Seconds = 0
)
$ErrorActionPreference = "Stop"
"time,api_mb,worker_mb,ffmpeg_mb,ffmpeg_count,total_mb" | Out-File -FilePath $Out -Encoding ascii
$start = Get-Date
while ($Seconds -le 0 -or ((Get-Date) - $start).TotalSeconds -lt $Seconds) {
    $procs = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='ffmpeg.exe'"
    $api = ($procs | Where-Object { $_.CommandLine -like '*uvicorn*app.main*' } | Measure-Object WorkingSetSize -Sum).Sum
    $worker = ($procs | Where-Object { $_.CommandLine -like '*app.worker*' -or $_.CommandLine -like '*fault_worker*' } |
        Measure-Object WorkingSetSize -Sum).Sum
    $ff = @($procs | Where-Object { $_.Name -eq 'ffmpeg.exe' })
    $ffmb = ($ff | Measure-Object WorkingSetSize -Sum).Sum
    $mb = { param($b) [math]::Round(([double]$b) / 1MB, 1) }
    $line = "{0},{1},{2},{3},{4},{5}" -f (Get-Date).ToUniversalTime().ToString("o"), (& $mb $api), (& $mb $worker),
        (& $mb $ffmb), $ff.Count, (& $mb ($api + $worker + $ffmb))
    # Another program (editor, indexer, antivirus) may hold the file for a moment: retry, never stop sampling.
    for ($try = 0; $try -lt 20; $try++) {
        try { Add-Content -Path $Out -Value $line -Encoding ascii -ErrorAction Stop; break }
        catch { Start-Sleep -Milliseconds 50 }
    }
    Start-Sleep -Milliseconds ([int]($Interval * 1000))
}
