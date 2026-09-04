<#
.SYNOPSIS
    Keep the teams-interface REST bridge running across logons.

.DESCRIPTION
    Registers a Scheduled Task that starts .\teams-api.ps1 at logon, hidden,
    logging to .\out\teams-interface.log. No admin rights needed: the task runs
    as you, in your own session, which is what the browser needs anyway.

.EXAMPLE
    .\service.ps1 install      # start at every logon, and start it now
    .\service.ps1 status       # is the task registered, is the server answering
    .\service.ps1 restart
    .\service.ps1 stop
    .\service.ps1 uninstall
#>
[CmdletBinding()]
param(
    [ValidateSet('install', 'uninstall', 'start', 'stop', 'restart', 'status', 'log')]
    [string]$Action = 'status',
    [string]$TaskName = 'teams-interface',
    [int]$Tail = 40
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$logDir = Join-Path $root 'out'
$log = Join-Path $logDir 'teams-interface.log'

function Get-Port {
    $cfgPath = Join-Path $root 'teams-interface.json'
    if (Test-Path $cfgPath) {
        try { return (Get-Content $cfgPath -Raw | ConvertFrom-Json).port } catch {}
    }
    return 8787
}

function Format-TaskResult {
    <#  Task Scheduler reports these as bare unsigned decimals, and the
        interesting ones are not guessable. 0xC000013A in particular is the
        symptom of running under a console: closing the window Ctrl-C's the
        server.  #>
    param($Code)
    switch ([uint32]$Code) {
        0          { "ok" }
        267009     { "running" }                     # 0x00041301
        267011     { "not yet run" }                 # 0x00041303
        2147946720 { "already running (ignored)" }   # 0x800710E0
        3221225786 { "KILLED by Ctrl-C / console close" }  # 0xC000013A
        default    { "0x{0:X8}" -f [uint32]$Code }
    }
}

function Get-PortHolder {
    $held = Get-NetTCPConnection -LocalPort (Get-Port) -State Listen -ErrorAction SilentlyContinue
    if ($held) { return $held[0].OwningProcess }
    return $null
}

function Wait-PortFree {
    param([int]$TimeoutSec = 20)
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if (-not (Get-PortHolder)) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

function Stop-Bridge {
    <#  Stop-ScheduledTask only stops what the task itself launched. Whatever
        is actually listening is the thing that must die, so go by the port. #>
    try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction Stop } catch {}
    if (Wait-PortFree -TimeoutSec 6) { return $true }
    $holder = Get-PortHolder
    if ($holder) {
        Write-Host "port $(Get-Port) still held by PID $holder; stopping it" -ForegroundColor Yellow
        Stop-Process -Id $holder -Force -ErrorAction SilentlyContinue
    }
    return (Wait-PortFree -TimeoutSec 10)
}

function Test-Server {
    $port = Get-Port
    try {
        $r = Invoke-RestMethod -Uri "http://127.0.0.1:$port/health" -TimeoutSec 4
        return $r
    } catch { return $null }
}

switch ($Action) {

    'install' {
        New-Item -ItemType Directory -Force -Path $logDir | Out-Null
        # Run the interpreter directly, and let `serve --log` write the file
        # itself. A PowerShell wrapper doing `*>> log` would be one more
        # process between the task and the server: stopping the task kills the
        # wrapper, the server survives with a dead stdout, and it goes on
        # holding the port without being able to answer anything.
        # pythonw.exe, not python.exe: a console app started by the task in an
        # interactive session gets a real console window, and closing that
        # window sends Ctrl-C and kills the server. pythonw has no console at
        # all -- nothing to show, nothing to close. It also has no stdout,
        # which is exactly why `serve --log` opens the log file itself.
        $taskAction = New-ScheduledTaskAction `
            -Execute (Join-Path $root '.venv\Scripts\pythonw.exe') `
            -Argument "-X utf8 -m teams_browser serve --quiet --log `"$log`"" `
            -WorkingDirectory $root

        $atLogon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
        # Watchdog: re-run every 5 minutes forever. Combined with
        # MultipleInstances=IgnoreNew below, a run while the server is alive is
        # discarded, and a run after it died brings it straight back -- so a
        # crash costs at most five minutes, with no extra moving parts.
        $watchdog = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
            -RepetitionInterval (New-TimeSpan -Minutes 5)
        $taskTrigger = @($atLogon, $watchdog)
        # The browser it drives lives in this user's session, so the task must
        # run interactively as the same user -- never as SYSTEM.
        $taskPrincipal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
            -LogonType Interactive -RunLevel Limited
        $taskSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
            -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
            -StartWhenAvailable   # run a watchdog tick the machine slept through
        Register-ScheduledTask -TaskName $TaskName -Action $taskAction -Trigger $taskTrigger `
            -Principal $taskPrincipal -Settings $taskSettings -Force | Out-Null
        Write-Host "registered scheduled task '$TaskName' (runs at logon as $env:USERNAME)" -ForegroundColor Green
        Write-Host "log: $log"
        Start-ScheduledTask -TaskName $TaskName
        Start-Sleep -Seconds 6
        & $PSCommandPath -Action status -TaskName $TaskName
    }

    'uninstall' {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "removed scheduled task '$TaskName'" -ForegroundColor Green
        Write-Host "(a server already running keeps running until you stop it)"
    }

    'start'   { Start-ScheduledTask -TaskName $TaskName; Write-Host "started" }

    'stop'    {
        if (Stop-Bridge) { Write-Host "stopped" }
        else { Write-Host "port $(Get-Port) is still held" -ForegroundColor Yellow }
    }

    'restart' {
        # Starting before the port is free gets "cannot bind" and leaves
        # nothing running at all, so the stop has to be real first.
        if (-not (Stop-Bridge)) {
            Write-Host "port $(Get-Port) still in use; not starting a second one" -ForegroundColor Yellow
            break
        }
        Start-ScheduledTask -TaskName $TaskName
        Write-Host "restarted"
    }

    'log' {
        if (Test-Path $log) { Get-Content $log -Tail $Tail }
        else { Write-Host "no log yet at $log" -ForegroundColor Yellow }
    }

    'status' {
        $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if ($task) {
            $info = Get-ScheduledTaskInfo -TaskName $TaskName
            Write-Host ("task:   {0}  (last run {1}, result: {2})" -f `
                    $task.State, $info.LastRunTime, (Format-TaskResult $info.LastTaskResult))
            Write-Host ("        triggers: at logon + watchdog every {0}min; next {1}" -f `
                    5, $info.NextRunTime)
        } else {
            Write-Host "task:   not registered  (run: .\service.ps1 install)" -ForegroundColor Yellow
        }
        $health = Test-Server
        if ($health) {
            Write-Host ("server: up on port {0}  browser={1} watcher={2} queue={3} pending" -f `
                (Get-Port), $health.browser_running, $health.watcher_running, $health.queue.pending) -ForegroundColor Green
        } else {
            Write-Host ("server: not answering on port {0}" -f (Get-Port)) -ForegroundColor Yellow
            if (Test-Path $log) { Write-Host "last log lines:"; Get-Content $log -Tail 10 }
        }
    }
}
