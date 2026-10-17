# Print the exact Windows peak working set (highest RAM in use since the process started) of the
# running API and worker Python processes. Peak values reset only when the process restarts.
#
#   powershell -File tools\process_peaks.ps1
$procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like '*uvicorn*app.main*' -or $_.CommandLine -like '*app.worker*' -or
                   $_.CommandLine -like '*fault_worker*' }
foreach ($p in $procs) {
    $g = Get-Process -Id $p.ProcessId -ErrorAction SilentlyContinue
    if ($g) {
        $role = if ($p.CommandLine -like '*app.main*') { "api" } else { "worker" }
        "{0},pid={1},parent={2},peak_mb={3},now_mb={4}" -f $role, $p.ProcessId, $p.ParentProcessId,
            [math]::Round($g.PeakWorkingSet64 / 1MB, 1), [math]::Round($g.WorkingSet64 / 1MB, 1)
    }
}
