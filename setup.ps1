<#
.SYNOPSIS
    One-shot Windows setup for teams-browser.

.DESCRIPTION
    Assumes nothing is installed. Finds or installs Python, creates the virtual
    environment, installs dependencies, and checks that a Chromium-based browser
    is present. Safe to re-run: every step is skipped if it is already done.

.PARAMETER WithPlaywrightBrowser
    Also download Playwright's bundled Chromium (~150 MB). Only needed for
    `--mode persistent`; the default CDP mode drives your installed Chrome/Edge.

.PARAMETER Launch
    After setup, open the debuggable browser so you can sign in to Teams.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\setup.ps1 -Launch
#>
[CmdletBinding()]
param(
    [switch]$WithPlaywrightBrowser,
    [switch]$Launch
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$MinPython = [version]'3.9'

function Write-Step { param($m) Write-Host "`n==> $m" -ForegroundColor Cyan }
function Write-Ok   { param($m) Write-Host "    OK  $m" -ForegroundColor Green }
function Write-Warn { param($m) Write-Host "    !!  $m" -ForegroundColor Yellow }

function Get-PythonVersion {
    param([string]$Exe)
    try {
        $raw = & $Exe -c "import sys;print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and $raw) { return [version]$raw.Trim() }
    } catch { }
    return $null
}

# Find any Python new enough, trying the launcher, PATH, then the usual homes.
function Find-Python {
    $candidates = @()
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        foreach ($tag in @('-3.13', '-3.12', '-3.11', '-3')) {
            try {
                $p = & $py.Source $tag -c "import sys;print(sys.executable)" 2>$null
                if ($LASTEXITCODE -eq 0 -and $p) { $candidates += $p.Trim() }
            } catch { }
        }
    }
    $onPath = Get-Command python -ErrorAction SilentlyContinue
    if ($onPath) { $candidates += $onPath.Source }
    $candidates += Get-ChildItem -ErrorAction SilentlyContinue -Path @(
        "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe",
        "$env:ProgramFiles\Python3*\python.exe"
    ) | ForEach-Object { $_.FullName }

    foreach ($c in ($candidates | Where-Object { $_ } | Select-Object -Unique)) {
        # The Store stub named python.exe exits nonzero and installs nothing.
        if ($c -like "*WindowsApps*") { continue }
        $v = Get-PythonVersion $c
        if ($v -and $v -ge $MinPython) { return [pscustomobject]@{ Exe = $c; Version = $v } }
    }
    return $null
}

function Install-Python {
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) {
        throw ("No Python >= $MinPython found and winget is unavailable. " +
               "Install Python from https://www.python.org/downloads/windows/ " +
               "(tick 'Add python.exe to PATH'), then re-run this script.")
    }
    Write-Warn "No suitable Python found; installing Python 3.12 via winget..."
    & winget install --id Python.Python.3.12 --source winget `
        --accept-package-agreements --accept-source-agreements --silent
    if ($LASTEXITCODE -ne 0) {
        throw "winget could not install Python (exit $LASTEXITCODE). Install it manually and re-run."
    }
    # winget updates the machine PATH, not this already-running shell.
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                [Environment]::GetEnvironmentVariable('Path', 'User')
}

# --- 1. Python ------------------------------------------------------------
Write-Step "Checking for Python >= $MinPython"
$python = Find-Python
if (-not $python) {
    Install-Python
    $python = Find-Python
    if (-not $python) {
        throw "Python still not found after install. Open a new terminal and re-run this script."
    }
}
Write-Ok "Python $($python.Version) at $($python.Exe)"

# --- 2. Virtual environment ----------------------------------------------
$venv = Join-Path $root '.venv'
$venvPy = Join-Path $venv 'Scripts\python.exe'
Write-Step "Setting up the virtual environment"
if (Test-Path $venvPy) {
    Write-Ok "Reusing $venv"
} else {
    & $python.Exe -m venv $venv
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $venvPy)) {
        throw "Failed to create the virtual environment at $venv"
    }
    Write-Ok "Created $venv"
}

# --- 3. Dependencies ------------------------------------------------------
Write-Step "Installing dependencies"
& $venvPy -m pip install --upgrade pip --quiet
& $venvPy -m pip install -r (Join-Path $root 'requirements.txt') --quiet
if ($LASTEXITCODE -ne 0) { throw "pip install failed (exit $LASTEXITCODE)" }
# Single-quoted inside: PowerShell mangles embedded double quotes when it
# hands the string to a native command.
$probe = "import importlib.metadata as m;print(m.version('playwright'))"

# A native command writing to stderr would otherwise trip -ErrorAction Stop.
$ver = $(try { & $venvPy -c $probe 2>$null } catch { $null })
if ($ver) { Write-Ok "playwright $ver" } else { throw "playwright did not import from $venvPy" }

if ($WithPlaywrightBrowser) {
    Write-Step "Downloading Playwright's Chromium (only needed for --mode persistent)"
    & $venvPy -m playwright install chromium
    if ($LASTEXITCODE -ne 0) { Write-Warn "Chromium download failed; CDP mode still works." }
    else { Write-Ok "Chromium ready" }
}

# --- 4. A browser to drive ------------------------------------------------
Write-Step "Looking for Chrome or Edge"
$browsers = @(
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
    "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe"
) | Where-Object { Test-Path $_ }
if ($browsers) {
    Write-Ok ([IO.Path]::GetFileName($browsers[0]) + " at " + $browsers[0])
} else {
    Write-Warn "Neither Chrome nor Edge found. Teams web needs one of them (Firefox is blocked)."
}

# --- 5. Done --------------------------------------------------------------
Write-Step "Setup complete"
if ($Launch) {
    & (Join-Path $root 'teams.ps1') launch
    Write-Host "`nSign in to Teams in the window that just opened, then run:" -ForegroundColor Cyan
    Write-Host "    .\teams.ps1 wait-login"
    Write-Host "    .\teams.ps1 chats"
} else {
    Write-Host @"

Next steps:
    .\teams.ps1 launch        # opens a debuggable browser on its own profile
    (sign in to Teams in that window -- this tool never handles credentials)
    .\teams.ps1 wait-login    # blocks until the chat list renders
    .\teams.ps1 chats         # list your conversations

To expose it on the LAN as http://teams-interface.local:8787/ :
    .\teams-api.ps1           # prints its API token; GET / documents itself

If PowerShell blocks these scripts, run them as:
    powershell -ExecutionPolicy Bypass -File .\teams.ps1 chats
"@ -ForegroundColor Cyan
}
