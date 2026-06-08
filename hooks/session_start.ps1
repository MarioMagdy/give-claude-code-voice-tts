# SessionStart hook — ensures the speech daemon is running, and (when speech is
# enabled for THIS project) rolls a fresh session voice.
#
# Reads the hook payload from stdin to learn the project cwd, so when this hook
# is installed GLOBALLY the voice roll targets the project the user is actually
# in, not this script's repo. voices.py session-random self-gates on the
# resolved `enabled` flag, so off-by-default projects get no roll and no
# session-state.json written into them.
#
# Daemon spawn is idempotent and cheap when already up (one stat + one ping).

$ErrorActionPreference = 'SilentlyContinue'

function Test-LocalPath($p) {
    # False for UNC paths -- \\host, //host, and the mixed forms /\ and \/ -- and empty.
    # Windows normalizes any two leading path separators to a UNC path, which would
    # trigger an outbound SMB read (NTLM-hash leak). Mirrors the Python _is_local_path guard.
    if ([string]::IsNullOrEmpty($p)) { return $false }
    if ($p.Length -ge 2 -and ($p[0] -eq '\' -or $p[0] -eq '/') -and ($p[1] -eq '\' -or $p[1] -eq '/')) { return $false }
    return $true
}

$here   = $PSScriptRoot
$state  = Join-Path $here 'daemon.state'
$daemon = Join-Path $here 'daemon.py'

# --- project dir from the hook payload (fallback: current location) ---
$projectDir = $null
try {
    if ([Console]::IsInputRedirected) {
        $raw = [Console]::In.ReadToEnd()
        if ($raw) { $projectDir = ($raw | ConvertFrom-Json).cwd }
    }
} catch { }
if (-not $projectDir) { $projectDir = (Get-Location).Path }
# Guard: don't hand a UNC / mixed-slash project dir to voices.py (it would read settings over SMB).
if (-not (Test-LocalPath $projectDir)) { $projectDir = $null }

function Test-DaemonAlive {
    if (-not (Test-Path $state)) { return $false }
    try {
        $info = Get-Content $state -Raw -ErrorAction Stop | ConvertFrom-Json
        $port = [int]$info.port
        if ($port -le 0) { return $false }
        $resp = Invoke-WebRequest -Uri "http://127.0.0.1:$port/ping" `
                                  -TimeoutSec 1 -UseBasicParsing -ErrorAction Stop
        return ($resp.StatusCode -eq 200)
    } catch { return $false }
}

# --- ensure the daemon (singleton; state file lives next to this script) ---
if (-not (Test-DaemonAlive)) {
    $pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
    if ($pythonw) {
        # Quote $daemon: the install path may contain spaces.
        Start-Process -FilePath $pythonw -ArgumentList "`"$daemon`"" -WindowStyle Hidden | Out-Null
        $deadline = (Get-Date).AddMilliseconds(3000)
        while ((Get-Date) -lt $deadline) {
            if (Test-Path $state) {
                Start-Sleep -Milliseconds 50
                if (Test-DaemonAlive) { break }
            }
            Start-Sleep -Milliseconds 50
        }
    }
}

# --- roll a fresh session voice for THIS project (no-op where disabled) ---
$python   = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
$voicesPy = Join-Path $here 'voices.py'
if ($python -and $projectDir -and (Test-Path $voicesPy)) {
    Start-Process -FilePath $python `
                  -ArgumentList @("`"$voicesPy`"", "--project", "`"$projectDir`"", "session-random") `
                  -WindowStyle Hidden | Out-Null
}

exit 0
