# SessionEnd hook — fired when Claude Code exits cleanly. Tells the daemon
# to stop any in-progress speech + drain the queue so audio doesn't keep
# playing after you /exit. Doesn't help with force-kill (X button) — for
# that, press the hotkey (default Ctrl+Alt+S) or run `voices.py silence`.

$ErrorActionPreference = 'SilentlyContinue'

$state = Join-Path $PSScriptRoot 'daemon.state'
if (-not (Test-Path $state)) { exit 0 }

try {
    $info = Get-Content $state -Raw -ErrorAction Stop | ConvertFrom-Json
    $port = [int]$info.port
    $token = [string]$info.token
    if ($port -le 0 -or -not $token) { exit 0 }
} catch { exit 0 }

# Empty POST to /cancel — daemon stops MCI playback + clears queue.
& curl.exe -s -o NUL -4 -X POST "http://127.0.0.1:$port/cancel" `
    -H "X-Speech-Token: $token" --max-time 2 | Out-Null
exit 0
