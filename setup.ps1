# MAI2API one-time setup (Windows PowerShell 5.1+).
# Run from this folder:  powershell -ExecutionPolicy Bypass -File .\setup.ps1
# Idempotent: safe to re-run.
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

# --- pick a python (windowless preferred for the background pieces) ---
$pyw = $null
$cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
if ($cmd) { $pyw = $cmd.Source }
if (-not $pyw) {
    $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($cmd) { $pyw = $cmd.Source }
}
if (-not $pyw) { throw "Python 3 was not found on PATH. Install Python 3 first." }
$py = $pyw -replace "pythonw\.exe$", "python.exe"
Write-Host "Python: $py"

# --- runtime state directory (outside the source tree) ---
# Token, browser profile, sessions and logs live in
# %LOCALAPPDATA%\MAI2API (or $env:MAI2API_STATE_DIR when set), so the
# source folder never holds live credentials.
$state = $env:MAI2API_STATE_DIR
if (-not $state) { $state = Join-Path $env:LOCALAPPDATA "MAI2API" }
New-Item -ItemType Directory -Path $state -Force | Out-Null
# One-time migration from older installs that kept state in-tree:
foreach ($name in "token.txt", "sessions.json", "notify_config.json",
                   "keeper_state.json", "rpa_hints.json") {
    $old = Join-Path $here $name
    $new = Join-Path $state $name
    if ((Test-Path $old) -and -not (Test-Path $new)) {
        Copy-Item $old $new
        Write-Host "Migrated $name to the state directory."
    }
}
$oldProfile = Join-Path $here "edge-profile"
$newProfile = Join-Path $state "edge-profile"
if ((Test-Path $oldProfile) -and -not (Test-Path $newProfile)) {
    Copy-Item $oldProfile $newProfile -Recurse
    Write-Host "Migrated the browser profile to the state directory."
}
Write-Host "State directory: $state"

# --- dedicated virtual environment (in the state directory) ---
# The components run from a private venv with pinned dependencies:
# never from whatever interpreter happens to be on PATH, and never
# from another project's environment.
$venv = Join-Path $state ".venv"
$venvPyw = Join-Path $venv "Scripts\pythonw.exe"
if (-not (Test-Path $venvPyw)) {
    Write-Host "Creating the dedicated venv in the state directory..."
    & $py -m venv $venv
}
if (Test-Path $venvPyw) {
    & (Join-Path $venv "Scripts\python.exe") -m pip install -r (Join-Path $here "requirements.txt") | Out-Null
    $pyw = $venvPyw
    $py = Join-Path $venv "Scripts\python.exe"
    Write-Host "Venv: $venv"
} else {
    Write-Host "WARNING: venv creation failed; falling back to $py"
    & $py -c "import websockets" 2>$null
    if ($LASTEXITCODE -ne 0) { & $py -m pip install -r (Join-Path $here "requirements.txt") }
}

# --- auto-start at logon (HKCU Run keys, no elevation needed) ---
# The guardian supervises the proxy; the keeper supervises the token.
$runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$guardian = Join-Path $here "guardian.py"
$keeper = Join-Path $here "rpa_keeper.py"
Set-ItemProperty -Path $runKey -Name "MAI2API Proxy" -Value "`"$pyw`" `"$guardian`""
Set-ItemProperty -Path $runKey -Name "MAI2API Keeper" -Value "`"$pyw`" `"$keeper`" --loop"
Write-Host "Auto-start registered (HKCU Run: MAI2API Proxy / MAI2API Keeper)."

# --- start both now ---
Start-Process -FilePath $pyw -ArgumentList "`"$guardian`"" -WorkingDirectory $here -WindowStyle Hidden
Start-Process -FilePath $pyw -ArgumentList "`"$keeper`" --loop" -WorkingDirectory $here -WindowStyle Hidden
Start-Sleep -Seconds 5
try {
    $h = Invoke-RestMethod -Uri "http://127.0.0.1:18787/health" -TimeoutSec 5
    Write-Host "Proxy health: $($h | ConvertTo-Json -Compress)"
} catch {
    Write-Host "Proxy did not answer yet; check proxy.log in this folder."
}

Write-Host ""
Write-Host "Remaining one-time manual step:"
Write-Host "  Sign in to the dedicated browser profile once:"
Write-Host "    python harvest_now.py --setup"
Write-Host "  A browser window opens on the Microsoft Playground; sign in with"
Write-Host "  your Microsoft account and close the window. From then on the"
Write-Host "  keeper renews the token unattended (headless), the guardian"
Write-Host "  restarts the proxy if it ever dies, and you can verify with:"
Write-Host "    python selftest.py"
Write-Host "  Daily use: run mai-claude.cmd to start Claude Code on MAI."
