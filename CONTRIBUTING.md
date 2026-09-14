# Contributing to give-claude-code-voice-tts

Thanks for looking at this. It's a small plugin with a narrow job — a Stop
hook, a localhost daemon, and a skill that controls both — so most changes
touch one or two files. This doc orients you before you dive in.

## Dev environment

This plugin targets **Windows** end to end: playback goes through
`winmm.dll` via `ctypes.windll.winmm.mciSendStringW` (see `hooks/daemon.py`,
`hooks/hook_worker.py`, `hooks/voices.py`), and the SessionStart/SessionEnd
hooks are PowerShell scripts. You don't need Windows to work on most of the
Python (see "Running the tests" below), but you do need it to hear anything
actually play, or to exercise the PowerShell hooks.

- **Python 3.10+**
- `pip install edge-tts` — the neural TTS engine; no account or API key
- Optional: `pip install keyboard` — only needed for the `Ctrl+Alt+S`
  global-hotkey interrupt (`_hotkey_listener` in `hooks/daemon.py`); its
  absence is a silent no-op, not an error
- `pip install pytest` to run the test suite

There's no build step and no other third-party runtime dependency — keep it
that way (see "What we're wary of" below).

## Running the tests

```text
pytest hooks/
```

The suite lives next to the code it tests: `hooks/test_daemon.py`,
`hooks/test_daemon_auth.py`, `hooks/test_voices.py`. Be honest with yourself
about platform sensitivity before you trust a green run:

- Most of the suite (config precedence, UNC/path-rejection logic, the
  `_log_job` contract, `voices.py` enable/disable/session-random behavior) is
  plain Python against stdlib `json`/`pathlib`/`subprocess` and runs fine on
  Linux/macOS.
- `hooks/test_daemon_auth.py` boots a **real daemon subprocess** and drives
  it over HTTP (`/stop`, `/cancel`, `/shutdown`, token checks). That part is
  platform-portable too, since the HTTP surface never touches `winmm`.
- One test, `test_is_local_path_rejects_mixed_slash_unc` in
  `hooks/test_daemon.py`, encodes a **Windows-specific** path-normalization
  fact (`os.path.normpath` turning `/\evil\share` into a UNC path). It
  currently fails on Linux/macOS because `os.path.normpath` doesn't do that
  fold there — the guard code itself (`_is_local_path`) is fine; the test's
  own sanity assertion about `normpath` is what's platform-bound. Don't
  "fix" the guard to chase this on non-Windows; if you touch it, treat it as
  a test-only Windows assumption.
- Nothing in the suite actually calls `_play_mci` / `mciSendStringW`, so you
  won't get real audio playback coverage off Windows regardless of pytest's
  exit code. If you're changing playback, verify by ear on Windows.

## Trying a local checkout as a plugin (no publishing needed)

`/plugin marketplace add` accepts a local path, and this repo's own
`.claude-plugin/marketplace.json` points its one plugin entry at `"./"` (the
repo root), with plugin id `voice-tts`. So from a checkout you can add it as
a marketplace source directly instead of pointing at the GitHub repo:

```text
/plugin marketplace add /absolute/path/to/give-claude-code-voice-tts
/plugin install voice-tts@give-claude-code-voice-tts
```

Once installed this way, Claude Code sets `$CLAUDE_PLUGIN_ROOT` to your
checkout, and every hook command and the `speech` skill resolve
`hooks/voices.py` etc. through that variable (see `hooks/hooks.json` and
`skills/speech/SKILL.md`) — so edits to your working copy take effect on the
next session without reinstalling. `/speech` (or `voices.py enable --pin`)
turns speech on for whatever project you test in.

## Architecture orientation

Start from `hooks/hooks.json` — it wires three lifecycle events to five
scripts, and that wiring is the whole shape of the plugin:

| Event | Script | Job |
|---|---|---|
| `SessionStart` | `hooks/session_start.ps1` | Ensures the daemon is running (spawns `daemon.py` via `pythonw.exe` if `daemon.state` shows it's not), then rolls a fresh session voice via `voices.py session-random` for the current project |
| `SessionStart` | `hooks/speech_status.ps1` | Synchronous — resolves whether speech is enabled for this project (session-state > project settings > global settings > off) and, only if on, injects the full `<spoken>` emission rule as additional context. This is what makes the plugin self-contained (no global CLAUDE.md edit needed) |
| `Stop` | `hooks/hook_post.py` | Reads the hook payload from stdin, looks up the daemon's port + auth token in `hooks/daemon.state`, and POSTs the payload to the daemon's `/stop` endpoint. Stdlib-only (`json` + `urllib`), fails silently so a dead daemon never blocks your next prompt |
| `SessionEnd` | `hooks/session_end.ps1` | Stops any audio in flight on exit |

Beyond the hook wiring:

- **`hooks/daemon.py`** — the persistent server. Holds a warm interpreter and
  a pre-imported `edge_tts`, listens on `127.0.0.1` on an ephemeral port,
  serializes jobs through one bounded queue (so overlapping sessions never
  talk over each other), and does the actual `winmm` playback. Port + PID +
  a random auth token live in `hooks/daemon.state`, written owner-only
  (`0o600`); every `/stop`, `/cancel`, `/shutdown` call must present that
  token via `X-Speech-Token`. This is the file to read first if you're
  touching security, concurrency, or the config-resolution precedence.
- **`hooks/hook_worker.py`** — an older direct-spawn synth+playback path
  (no daemon, no HTTP). Kept for the SAPI/Piper fallback story in
  `CONTINGENCIES.md`; the daemon is the default architecture now.
- **`hooks/voices.py`** — the control engine behind the `speech` skill and
  `/speech` menu: `enable`/`disable`/`set`/`match`/`session-random`/`silence`/
  `current`/`list`/`preview`/`sample`. Understands the same ephemeral
  (`.claude/session-state.json`) vs. pinned (`.claude/settings.json`)
  distinction the daemon does.
- **`hooks/session_start.ps1`** / **`hooks/speech_status.ps1`** — both parse
  the hook's stdin JSON for `cwd` and both run a `Test-LocalPath` guard
  first, rejecting UNC and mixed-slash-UNC paths before touching the
  filesystem (an SMB/NTLM-hash-leak guard — see git history and
  `CONTINGENCIES.md`). If you add a third PowerShell hook that dereferences
  `cwd`, it needs the same guard.
- **`skills/speech/SKILL.md`** — the natural-language control surface: the
  dispatch table mapping what a user says to which `voices.py` subcommand,
  the interactive-menu behavior, and the ephemeral-vs-pinned explanation.
  If you add or rename a `voices.py` subcommand, update this table too.
- **`CONTINGENCIES.md`** — pre-decided fallbacks for known failure modes
  (edge-tts breaking, latency, double-firing hooks, markdown leaking into
  speech, concurrent sessions, `<spoken>` reliability drift). Read it before
  reinventing a fix for something that already has a documented answer.

## What's most wanted

Per the README's FAQ, **macOS/Linux playback support is the single highest-value
contribution** this project can take. The synthesis step (`edge-tts`) is
already cross-platform; only the playback call
(`ctypes.windll.winmm.mciSendStringW`) is Windows-only, isolated in one
function per file (`_play_mci` in `hooks/daemon.py` and
`hooks/hook_worker.py`, `hooks/voices.py`'s preview/sample playback). A
contribution that swaps in a portable player behind that same interface
(and updates `hooks/session_end.ps1`'s Windows-specific stop-on-exit
behavior) would remove the platform restriction outright. If you take this
on, please also update the README's requirements/FAQ and
`.claude-plugin/plugin.json` accordingly, since both currently advertise
Windows-only.

Smaller wanted contributions: more curated voices in `voices.py`'s
catalogue, additional `match` vibe/synonym coverage, and hardening per
`CONTINGENCIES.md`'s open items (e.g. C5's named-mutex de-overlap, C6's
periodic `<spoken>`-rule reinjection).

## PR expectations

- `pytest hooks/` should stay green (see the platform caveat above — a
  failure isolated to `test_is_local_path_rejects_mixed_slash_unc` on a
  non-Windows box is a known, already-understood gap, not a regression you
  introduced; say so in the PR rather than silently working around it).
- No new third-party runtime dependency without discussion first — the
  project's whole pitch is "no cloud account, no API key," and `hook_post.py`
  in particular is deliberately stdlib-only so the Stop-hook path stays fast
  and dependency-free.
- `hooks/daemon.py` is security-sensitive: it's a localhost HTTP server that
  accepts a token and executes actions. Changes there (auth checks, body-size
  limits, the UNC/local-path guard, state-file permissions) deserve extra
  scrutiny and, ideally, a test alongside the code change (see
  `hooks/test_daemon_auth.py` for the pattern).
- Match the existing tone in this repo's docs: plain and direct, no emoji.

## Reporting a security issue

Please don't open a public issue for a security vulnerability (e.g. a way to
reach the daemon's endpoints without the auth token, an injection path
through the spoken text, or a path-traversal in the UNC guards). Use this
repository's **Security tab** (private vulnerability reporting) instead.
