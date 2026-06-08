# SessionStart hook (SYNCHRONOUS) — injects a one-line context fact so the
# model knows whether to emit <spoken> tags this session (self-gating).
#
# Must be sync (async hooks' stdout is NOT captured for additionalContext).
# Kept PowerShell-native (no Python spawn) so the per-session cost is ~200 ms,
# not ~500 ms. Resolves `enabled` with the SAME precedence as voices.py /
# daemon.py: session-state > project settings > global settings > default(off).
# When OFF, it prints nothing — absence of the line is the model's "don't emit"
# signal.
#
# NOTE (plugin form): this hook injects the FULL <spoken> emission rule inline,
# so the plugin is self-contained and needs no global CLAUDE.md entry.

$ErrorActionPreference = 'SilentlyContinue'

function Test-LocalPath($p) {
    # False for UNC paths -- \\host, //host, and the mixed forms /\ and \/ -- and empty.
    # Windows normalizes any two leading path separators to a UNC path, which would
    # trigger an outbound SMB read (NTLM-hash leak). Mirrors the Python _is_local_path guard.
    if ([string]::IsNullOrEmpty($p)) { return $false }
    if ($p.Length -ge 2 -and ($p[0] -eq '\' -or $p[0] -eq '/') -and ($p[1] -eq '\' -or $p[1] -eq '/')) { return $false }
    return $true
}

# --- project dir from the hook payload (fallback: current location) ---
$cwd = $null
try {
    if ([Console]::IsInputRedirected) {
        $raw = [Console]::In.ReadToEnd()
        if ($raw) { $cwd = ($raw | ConvertFrom-Json).cwd }
    }
} catch { }
if (-not $cwd) { $cwd = (Get-Location).Path }
# Guard: a UNC / mixed-slash $cwd must not reach Test-Path/Get-Content (SMB / NTLM leak). Default OFF.
if (-not (Test-LocalPath $cwd)) { exit 0 }

function Get-Enabled($cwd) {
    $layers = @(
        @{ path = (Join-Path $cwd '.claude\session-state.json'); nested = $false },
        @{ path = (Join-Path $cwd '.claude\settings.json');      nested = $true  },
        @{ path = (Join-Path $env:USERPROFILE '.claude\settings.json'); nested = $true }
    )
    foreach ($l in $layers) {
        if (Test-Path $l.path) {
            try {
                $j = Get-Content $l.path -Raw -ErrorAction Stop | ConvertFrom-Json
                $val = if ($l.nested) { $j.speech.enabled } else { $j.enabled }
                if ($val -is [bool]) { return $val }   # present at this layer → decisive
            } catch { }
        }
    }
    return $false  # OFF by default — opt-in per project (matches voices.py)
}

if (Get-Enabled $cwd) {
    Write-Output "Spoken output (text-to-speech) is ENABLED for this project. End each CONVERSATIONAL reply (an explanation, status update, recommendation, opinion, question, or confirmation) with a <spoken>...</spoken> summary at the very end, in plain prose with no markdown, code, tables, or URLs inside the tag - say it the way you would speak it aloud. OMIT the tag for data-shaped replies (tables, code, file dumps, raw command output, long lists) and for pure tool-call turns. The tag is plumbing: never narrate it, and put it only at the very end. Speech is OFF in projects where this line is absent - do not emit the tag there."
}
# OFF -> print nothing; the model defaults to not emitting.
exit 0
