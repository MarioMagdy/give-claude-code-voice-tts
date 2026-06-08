# Speech control skill — design

- **Date:** 2026-05-31
- **Status:** Approved (design); implementation pending
- **Branch:** `feat/speech-skill`

## Background

This repo gives Claude Code on Windows a spoken voice. Two layers exist today:

- **Always-on behavior** (`CLAUDE.md`): the assistant ends conversational
  replies with `<spoken>…</spoken>`; a Stop hook reads it and a persistent
  daemon (`hooks/daemon.py`) synthesises + plays it.
- **On-demand control** (`hooks/voices.py` + a dispatch table in `CLAUDE.md`):
  list / preview / sample / match-by-vibe / set / pin / session-random /
  silence. The dispatch table tells Claude which `voices.py` command to run
  for a given user phrasing.

The on-demand layer currently lives as a prose table inside `CLAUDE.md`. That
has two problems: it only loads when cwd is this repo, and it competes for
attention with the always-on rule instead of being matched on demand.

## Goal

Promote the on-demand control layer into a proper, reliably-triggered Claude
Code **skill**, covering four capabilities the user selected:

1. **Voice control** — set / pin / preview / sample / match-by-vibe / list.
2. **On/off toggle** — enable or disable spoken output, ephemeral + persistent.
3. **Auto-pick & project-fit** — roll a session voice; suggest a fitting voice
   for a new project from its README / CLAUDE.md.
4. **Interrupt / silence** — `voices.py silence` + the Ctrl+Alt+S hotkey,
   including the troubleshooting context.

## Non-goals (this spec)

- The always-on `<spoken>` emission rule stays in `CLAUDE.md` — skills only run
  when invoked, but the tag rule must apply to every reply. The skill does
  **not** own that behavior.
- The global multi-project queue is **captured as future work** (below), not
  built. Speech is still project-scoped (hooks fire only in this repo).

## Approach (chosen: A + C)

- **A — Thin dispatcher skill.** `voices.py` remains the single engine (all
  real logic, unit-testable). The skill is orchestration: it maps user intent →
  the correct `voices.py` invocation, and triggers reliably via its
  `description` frontmatter.
- **C — Trim `CLAUDE.md`.** Move the voice dispatch table out of `CLAUDE.md`
  into the skill so it isn't duplicated. `CLAUDE.md` keeps only the always-on
  `<spoken>` rules plus a one-line pointer to the skill.

Rejected — **B (thick self-contained skill** with its own bundled scripts):
duplicates `voices.py`, two maintenance points, breaks the one-engine model.

## Design

### 1. New skill — `.claude/skills/speech/SKILL.md`

Project-scoped (this repo), structured so it can be promoted to
`~/.claude/skills/` in Phase 2 alongside the hooks.

- **Frontmatter `description`** must trigger on the full range of speech/voice
  phrasings, e.g.: switch/change voice, what voices are there, current voice,
  preview/sample a voice, "calmer / more British / make it male / more
  expressive" (vibe), roll a new/random voice, stop speaking / shut up / be
  quiet / mute, speak again / unmute, turn speech on/off.
- **Body** = the dispatch table (user-says → `voices.py` command), the
  vibe-mapping rule of thumb, the on/off rules, the interrupt/silence section
  (incl. hotkey + "restart the daemon to pick up code/hotkey changes"), the
  ephemeral-vs-pinned explanation, and the project-fit suggestion guidance.
  All of this is migrated from the existing `CLAUDE.md` section, plus the two
  new commands below.

### 2. `hooks/voices.py` additions

The daemon already reads `enabled` from `.claude/session-state.json` as the
highest-precedence config layer (`_read_session_state` → `_load_config` →
`_process_job` checks `cfg["enabled"]`). So on/off needs no daemon change.

- `voices.py disable` → set `enabled: false` in `session-state.json`
  (ephemeral; outranks settings, takes effect next reply). `--pin` → set
  `speech.enabled = false` in project `settings.json` and clear the
  session-state override (mirrors the existing `_set_pinned` pattern for voice).
- `voices.py enable` → mirror: ephemeral sets `enabled: true` in session-state;
  `--pin` sets `speech.enabled = true` in settings and clears the override.
- `voices.py current` → additionally report resolved on/off state and its
  provenance (today it reports only the voice).

These reuse the existing `_read_json` / `_write_json` / precedence helpers, so
they are consistent with `set`/`match` and need no new config plumbing.

### 3. `CLAUDE.md` trim

- **Keep verbatim:** the entire `<spoken>` emission rules section (always-on)
  and the "no markdown inside `<spoken>`" rules.
- **Replace:** the "Voice switching helper" table and its sub-notes with a
  short pointer: for anything voice/speech-related (switching, vibe, on/off,
  preview, interrupt), use the speech skill.

## Data flow (unchanged by this spec)

User phrasing → skill triggers → Claude runs a `voices.py <cmd>` →
`voices.py` writes `session-state.json` / `settings.json` → daemon re-reads
config on the next job. No new runtime path; the skill is a router over the
existing engine.

## Testing

- **`voices.py` enable/disable unit checks:** `disable` then `current` reports
  off; `enable` reports on; `--pin` writes `settings.json` and clears the
  session-state override; ephemeral `disable` overrides a `settings.json`
  `enabled:true`. Assert exact file contents + precedence resolution.
- **Round-trip:** `disable` → daemon job is skipped (`_process_job` returns at
  the `enabled` check); `enable` → job plays again. Verifiable via `speech.log`
  status (`skip:disabled` vs `played`).
- **Skill trigger sanity (manual):** "make it calmer" fires the skill →
  `match "calm" --set`; "stop speaking" → `disable`; "what voices" → `list`.

## Future work (captured, not built): global multi-project queue

Today there is already a single serial queue: one daemon (a singleton located
via `daemon.state`), one `_job_q`, one clip at a time. Concurrent sessions are
already serialised — no overlap (this is what CONTINGENCIES C5 worried about).

The real gap for going global is **singleton discovery + queue policy**:

- **Singleton state path.** `daemon.state` currently lives in *this repo's*
  `hooks/`. Globally, two projects could each spawn their own daemon → two
  queues → overlap returns. Fix: a single well-known path (e.g.
  `~/.claude/speech/daemon.state`) so all projects share exactly one daemon.
- **Queue policy under load** (choose when global is real):
  - *Stale-drop:* skip a clip whose reply is older than N seconds (don't speak
    a reply from 15s ago).
  - *Ordering:* strict FIFO vs newest-wins (a new reply preempts a queued one).
  - *Source identity:* optionally signal which project is speaking (short
    earcon or per-project voice).
  - *Fairness:* cap per-project queue depth so one chatty project can't hog
    playback.

These are deferred until speech is promoted global (README Phase 2).

## Open questions

None blocking. The queue policy choices above are intentionally deferred.
