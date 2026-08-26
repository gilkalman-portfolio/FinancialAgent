# Soak-test memory logger for scheduler.py, added 2026-08-26 per an external
# design review (IdeaDistill panel) of the edgar_fcf/gc.collect() memory fixes
# from 2026-08-25/26: "run a 72-hour production soak with hourly RSS logging
# before declaring this resolved." Finds the live scheduler.py process by
# command line (not a fixed PID -- the process restarts on crash/deploy, see
# CLAUDE.md's crash history) and appends one CSV row per run. Registered as
# an hourly Windows Scheduled Task; see CLAUDE.md Incident Archive, 2026-08-26.

$logPath = "C:\Projects\FinancialAgent\logs\memory_soak.csv"

if (-not (Test-Path $logPath)) {
    "timestamp,pid,process_start,private_mb,working_set_mb" | Out-File -FilePath $logPath -Encoding utf8
}

$proc = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'scheduler\.py' } |
    Select-Object -First 1

if ($null -eq $proc) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss'),,,,NOT_RUNNING" | Out-File -FilePath $logPath -Append -Encoding utf8
    exit
}

$p = Get-Process -Id $proc.ProcessId -ErrorAction SilentlyContinue
if ($null -eq $p) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss'),$($proc.ProcessId),,,PID_GONE" | Out-File -FilePath $logPath -Append -Encoding utf8
    exit
}

$privateMb = [math]::Round($p.PrivateMemorySize64 / 1MB, 1)
$workingSetMb = [math]::Round($p.WorkingSet64 / 1MB, 1)
$startTime = $p.StartTime.ToString('yyyy-MM-dd HH:mm:ss')

"$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss'),$($p.Id),$startTime,$privateMb,$workingSetMb" |
    Out-File -FilePath $logPath -Append -Encoding utf8
