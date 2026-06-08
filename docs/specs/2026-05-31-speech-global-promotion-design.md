# Speech — global promotion design

- **Date:** 2026-05-31
- **Status:** Approved (design); implementation pending
- **Branch:** `feat/speech-skill` (or a follow-on branch)
- **Prior spec:** `2026-05-31-speech-skill-design.md` (the skill itself)

## Background

Speech (the `<spoken>` Stop-hook + edge-tts daemon + `voices.py` + the `speech`
skill) is currently **project-scoped**: hooks fire only inside this repo, and
`voices.py` reads/writes config under *this repo's* `.claude/`. The user wants
it available on **every project on the laptop**, but **off by default**, with
the ability to turn it on/off globally, per-project, or per-session.

## Goal

Promote the system to global with a quiet, opt-in posture.

## Decisions (from brainstorming)

1. **Default state:** OFF by default globally; **opt-in per project**.
2. **Voice:** random voice **each session**, but only in projects where speech
   is enabled (no littering `session-state.json` into off projects).
3. **`<spoken>` emission:** **self-gating** — only emit the tag where speech is
   enabled; the SessionStart hook tells me the state so I don't guess.
4. **Code layout:** scripts stay in this one repo; global config points at them
   by absolute path → a single shared daemon.

## Approach (chosen: A)

- **A — One script set, global config points at it.** Keep `daemon.py`,
  `hook_post.py`, `voices.py`, `session_start.ps1`, `session_end.ps1` in this
  repo. Global `~/.claude/settings.json` hooks and `~/.claude/skills/speech`
  reference them by absolute path. One script set → one `daemon.state` → one
  daemon for the whole laptop.
- **B — Copy scripts into `~/.claude` (rejected):** two copies to maintain,
  split git history, and re-creates the multi-daemon/overlap risk.

## Design

### 1. On/off via config precedence (mostly existing)

The daemon already resolves `enabled` with precedence
`session-state > project settings > global settings > hardcoded default(True)`.
Layered control falls out of this:

| Scope | File | Set via |
|---|---|---|
| Global default | `~/.claude/settings.json` → `speech.enabled` | `voices.py enable/disable --global` (NEW) |
| Per-project | `<project>/.claude/settings.json` → `speech.enabled` | `voices.py enable/disable --pin` |
| This session | `<project>/.claude/session-state.json` → `enabled` | `voices.py enable/disable` |

Going global sets `~/.claude/settings.json` → `speech.enabled: false`. A project
opts in with `enable --pin`. **New work:** add a `--global` scope to
`enable`/`disable` that writes `~/.claude/settings.json`.

### 2. cwd-correctness in `voices.py` (core code change)

Today `voices.py` derives `SESSION_STATE` / `PROJECT_SETTINGS` from the *script*
location (`HERE.parent` = this repo). Globally that's wrong — it must target the
project the user is in.

- Resolve the project dir from `Path.cwd()` (with an optional `--project DIR`
  override for hooks that know the cwd from their payload).
- `SESSION_STATE` = `<cwd>/.claude/session-state.json`,
  `PROJECT_SETTINGS` = `<cwd>/.claude/settings.json`.
- `USER_SETTINGS` stays `~/.claude/settings.json`.
- Affects `set`, `match`, `enable`, `disable`, `current`, `session-random`,
  `_resolve_voice`, `_resolve_enabled`. (The daemon is unaffected — it already
  uses the cwd from the hook payload.)

### 3. `session-random` gating

`session-random` becomes enabled-aware: resolve `enabled` for the cwd first; if
off, **no-op** (don't write `session-state.json`). This keeps off projects
clean. Where on, it rolls a fresh random English-friendly voice each session
(unchanged behavior).

### 4. Self-gating `<spoken>` emission + SessionStart context

- The global `<spoken>` rule (in `~/.claude/CLAUDE.md`) is phrased: *spoken
  output is opt-in and OFF by default; only emit `<spoken>` when speech is
  enabled for the current project; if off or unknown, don't emit.*
- To make this reliable, the **SessionStart hook injects the resolved state**
  into context: it runs `voices.py current --json` (NEW `--json` flag emitting
  `{"enabled": bool, "voice": "..."}`) for the cwd and returns
  `additionalContext` like `"Speech is ON for this project (voice: Ryan)."` or
  `"Speech is OFF for this project (opt-in with the speech skill)."`
- If the user enables/disables mid-session, I update based on that action.
- The daemon's `enabled` check remains the hard backstop regardless of emission.

### 5. Hooks → global

- Add `SessionStart`, `Stop`, `SessionEnd` to `~/.claude/settings.json` with
  absolute-path commands to this repo's scripts.
- **Remove** this repo's project-scoped `hooks` block from
  `<repo>/.claude/settings.json` to avoid double-fire (CONTINGENCIES C3). Keep
  the repo's `speech` block with `enabled: true` as the opt-in example (so the
  sandbox keeps speaking).
- SessionStart hooks must run with the project cwd (or pass `--project` from the
  hook payload) so the voice roll and state-injection target the right project.

### 6. Skill → global

Install the `speech` skill at `~/.claude/skills/speech` so it triggers in every
project. The repo remains the source of truth:
- **Preferred:** symlink `~/.claude/skills/speech` → `<repo>/.claude/skills/speech`
  (Windows: needs Developer Mode or admin for `New-Item -ItemType SymbolicLink`).
- **Fallback:** copy, with a note to re-copy on change.
- Skill command examples switch from `python hooks/voices.py …` to the **absolute
  path** to this repo's `voices.py` (cwd varies globally); `voices.py` then
  targets the cwd project per §2.

### 7. Single daemon

Automatic under Approach A: one script set → `daemon.state` always at
`<repo>/hooks/daemon.state` → every project's SessionStart pings/​spawns the same
daemon. No per-project daemons; no overlap. (Global queue *policy* — stale-drop,
fairness — remains future work per the prior spec.)

## Opt-in / opt-out cheat-sheet (end state)

- Turn ON for the project I'm in: speech skill → "turn on speech here", or
  `voices.py enable --pin`.
- Turn OFF that project again: `voices.py disable --pin`.
- Just this session: `voices.py enable` / `disable`.
- Flip the global default: `voices.py enable --global` / `disable --global`.
- Interrupt audio now: `voices.py silence` / Ctrl+Alt+S (unchanged).

## Testing

- **`voices.py` cwd-targeting (unit):** run from a temp dir; assert `set` /
  `enable` / `disable` write to `<tempcwd>/.claude/…`, not the repo. Extend
  `hooks/test_voices.py`.
- **`--global` scope (unit):** `enable/disable --global` writes
  `~/.claude/settings.json` (use a patched USER_SETTINGS temp path).
- **`current --json` (unit):** emits valid JSON with `enabled`/`voice`.
- **`session-random` gating (unit):** off → no `session-state.json` written;
  on → file written with a voice.
- **Manual end-to-end:** open a different project → silent, SessionStart context
  says OFF; `enable --pin` there → next reply speaks, context says ON; confirm
  this sandbox repo still speaks; confirm no double-fire (audio once).

## Risks & rollback

- **Blast radius:** purely additive to `~/.claude` (settings hooks + skill +
  `enabled:false`). Rollback = remove the global hooks block, the global skill,
  and the `speech` block from `~/.claude/settings.json`; the project sandbox is
  unaffected.
- **Double-fire:** mitigated by removing the repo's project hooks (§5).
- **cwd assumption:** if a hook runs with an unexpected cwd, the `--project`
  override (from the payload) is the robust fallback.
- **Symlink privilege:** if Windows symlink isn't permitted, fall back to copy.

## Out of scope / future

- Global queue **policy** (stale-drop, newest-wins, per-project earcon,
  fairness) — still future work; one serial queue is fine for one user.
- Promoting to other machines (this is laptop-local).

## Open questions

None blocking.
