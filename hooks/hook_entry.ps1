# Stop-hook entrypoint. Forwards the payload to the speech daemon via a
# single localhost POST. No Python spawn, no asyncio import — this script
# returns in tens of ms after PowerShell startup, well under the hook's
# 5 s timeout.
#
# If the daemon isn't running (no state file or unreachable), we exit silently:
# the SessionStart hook is responsible for keeping the daemon up, and TTS is
# non-critical — better to lose audio than to slow the user's next prompt.

$ErrorActionPreference = 'SilentlyContinue'

# Optional self-timing: set $env:CLAUDE_TTS_DEBUG=1 to log step timings to
# $env:TEMP\claude-tts-hook.log. Off by default for zero overhead.
$debug = $env:CLAUDE_TTS_DEBUG -eq '1'
$logPath = if ($debug) { Join-Path $env:TEMP 'claude-tts-hook.log' } else { $null }
$sw = [System.Diagnostics.Stopwatch]::StartNew()
function _log($msg) {
    if ($logPath) {
        Add-Content -LiteralPath $logPath -Value ("[{0,6:N0}ms] {1}" -f $sw.Elapsed.TotalMilliseconds, $msg)
    }
}
_log "start"

# 1. Capture stdin payload.
$payload = [Console]::In.ReadToEnd()
_log "stdin read ($($payload.Length) chars)"
if (-not $payload) { _log "no payload, exit"; exit 0 }

# 2. Look up the daemon's port from the state file next to this script.
$state = Join-Path $PSScriptRoot 'daemon.state'
if (-not (Test-Path $state)) { _log "no state file, exit"; exit 0 }

try {
    $info = Get-Content $state -Raw -ErrorAction Stop | ConvertFrom-Json
    $port = [int]$info.port
    $token = [string]$info.token
    if ($port -le 0) { _log "bad port, exit"; exit 0 }
    # SC4 — the daemon requires X-Speech-Token; without it the 401s would be
    # silent here (curl -s) and no audio would ever play. If we have no token,
    # exit 0 quietly (matches session_end.ps1 behaviour).
    if (-not $token) { _log "no token, exit"; exit 0 }
} catch {
    _log "state parse failed, exit"
    exit 0
}
_log "port=$port"

# 3. Persist payload to a temp file so curl can stream it via --data-binary
#    (avoids PowerShell mangling JSON on the command line).
$tmp = Join-Path $env:TEMP ("claude-tts-post-" + [guid]::NewGuid().ToString("N") + ".json")
[System.IO.File]::WriteAllText($tmp, $payload, [System.Text.UTF8Encoding]::new($false))
_log "wrote temp"

# 4. POST it. curl.exe is built into Windows 10/11; tiny startup, native HTTP.
#    -4 forces IPv4 (skips IPv6 fallback on localhost). SC4: send the
#    X-Speech-Token header that the daemon requires (read from daemon.state).
& curl.exe -s -o NUL -4 `
    -X POST "http://127.0.0.1:$port/stop" `
    -H "X-Speech-Token: $token" `
    -H 'Content-Type: application/json' `
    --data-binary "@$tmp" `
    --max-time 2 | Out-Null
_log "curl returned (exit=$LASTEXITCODE)"

Remove-Item -LiteralPath $tmp -ErrorAction SilentlyContinue
_log "done"
exit 0
