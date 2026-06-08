---
name: speech
description: Control Claude Code's spoken output (text-to-speech) on Windows — turn the voice on or off, mute or "stop speaking", switch or preview the TTS voice, change voice by vibe (calmer, more British, male, expressive), list or sample voices, or interrupt audio that is currently playing. Use when the user says "make Claude talk", "turn on TTS", "give Claude a voice", "stop speaking", "mute", "speak again", "change the voice", or "read your replies aloud". Windows edge-tts voice daemon.
when-to-use: Use to control the spoken-output system in this project. Trigger phrases — "turn speech on/off", "mute", "stop speaking", "speak again", "switch to <voice>", "calmer/more British/male voice", "list/preview/sample voices", "interrupt the audio".
---

# Speech — voice & spoken-output control

## Overview

This project speaks the `<spoken>…</spoken>` summary at the end of replies aloud
via a persistent edge-tts daemon. This skill is the **control surface** for that
system: pick/preview voices, switch by vibe, turn speech on or off, and stop
audio that's playing. The engine is `hooks/voices.py`; the daemon re-reads its
config on every job, so every change below takes effect on the *next* reply (no
restart).

**Not in scope:** emitting the `<spoken>` tag itself. That's handled automatically — a
SessionStart hook (`hooks/speech_status.ps1`) injects the `<spoken>` emission rule whenever
speech is enabled for the project, so the rule applies to every reply without this skill running.

## Interactive menu (when invoked with no specific request)

If the user opens this skill **without a concrete instruction** (bare `/speech`,
or "give me options" / "show me a menu"), don't guess — present a picker with
the `AskUserQuestion` tool. Open to the **action menu**, then drill into a
sub-picker based on the choice. (If the user already said something concrete —
"switch to Ryan", "stop speaking" — skip the menu and run it straight from the
dispatch table below.)

**Entry — "What would you like to do?"** (single-select):

| Choice | Then |
|---|---|
| Switch voice | → voice sub-picker |
| Change the vibe | → vibe sub-picker |
| Turn speech on / off | run `enable` or `disable` — offer the opposite of the current state (check `current` first if unsure) |
| Stop audio now | run `python "${CLAUDE_PLUGIN_ROOT}/hooks/voices.py" silence` |

**Voice sub-picker** (max 4 options + the auto "Other"): offer a curated spread,
e.g. Ryan (en-GB, calm male), Ava (en-US multilingual, expressive female),
Andrew (en-US multilingual, calm male), Sonia (en-GB, warm female). Tell the
user "Other" lets them type any voice id, ask for the Arabic voices, or hear
`sample`s. Apply the pick with `set <id>` (add `--pin` only if they want it
permanent).

**Vibe sub-picker:** Calmer · More expressive · Male · British (or similar) →
run `match "<chosen words>" --set`.

## Dispatch table — what the user says → what you run

> Installed as a plugin, Claude Code sets `$CLAUDE_PLUGIN_ROOT`, so `voices.py` is at
> `$CLAUDE_PLUGIN_ROOT/hooks/voices.py` (used below). From a plain clone, replace it with the
> absolute path to this repo's `hooks/voices.py`.

Run from anywhere — `voices.py` acts on your **current project** (cwd). Voice
ids look like `en-GB-RyanNeural`.

| User says | Run |
|---|---|
| "list voices" / "what voices are there" | `python "${CLAUDE_PLUGIN_ROOT}/hooks/voices.py" list` |
| "what voice am I using" / "current voice" / "is speech on?" | `python "${CLAUDE_PLUGIN_ROOT}/hooks/voices.py" current` |
| "preview Ryan" / "let me hear Salma" | `python "${CLAUDE_PLUGIN_ROOT}/hooks/voices.py" preview <id>` |
| "sample all / arabic / english voices" | `python "${CLAUDE_PLUGIN_ROOT}/hooks/voices.py" sample --lang en\|ar\|multi\|all` |
| "switch to <named voice>" | `python "${CLAUDE_PLUGIN_ROOT}/hooks/voices.py" set <id>` |
| "permanently switch to <voice>" / "pin this voice" | `python "${CLAUDE_PLUGIN_ROOT}/hooks/voices.py" set <id> --pin` |
| "calmer voice" / "more British" / "make it male" / "more expressive" | `python "${CLAUDE_PLUGIN_ROOT}/hooks/voices.py" match "<vibe words>" --set` |
| "roll a new random voice" / "different vibe" | `python "${CLAUDE_PLUGIN_ROOT}/hooks/voices.py" session-random` |
| "turn speech off" / "stop speaking" / "mute" / "be quiet" | `python "${CLAUDE_PLUGIN_ROOT}/hooks/voices.py" disable` |
| "speak again" / "turn speech back on" / "unmute" | `python "${CLAUDE_PLUGIN_ROOT}/hooks/voices.py" enable` |
| "permanently turn speech off / on" | `python "${CLAUDE_PLUGIN_ROOT}/hooks/voices.py" disable\|enable --pin` |
| "stop talking right NOW" / "shut up" (cut off audio mid-play) | `python "${CLAUDE_PLUGIN_ROOT}/hooks/voices.py" silence` (or the user presses Ctrl+Alt+S) |

## On/off vs interrupt — don't confuse them

- **disable / enable** = whether *future* replies are spoken. `disable` writes
  `enabled:false` so the daemon skips synthesis even if a `<spoken>` tag is
  present. (You may keep emitting the tag — it's harmless when disabled.)
- **silence** = stop the clip playing *right this second*. Doesn't change the
  on/off setting; the next reply still speaks.

If the user says "stop speaking" while audio is playing and wants future replies
silent too, do both: `silence` then `disable`.

## Ephemeral vs pinned

`set`, `match`, `enable`, `disable` default to **ephemeral** — written to
`.claude/session-state.json`, lasting this session only; a fresh `claude` re-rolls
a random voice and resets to enabled. Add `--pin` to write `.claude/settings.json`
**permanently** (and clear the session override so the pin wins). Default to
ephemeral unless the user says "permanently" / "always".

## Vibe mapping

`match` scores curated voices by tag overlap — just pass the user's words through.
Tags include `calm`, `expressive`, `warm`, `neutral`, `friendly`, `narrator`,
`focused`, plus `male`/`female`, accent (`american`/`british`/`australian`), and
language (`multilingual`/`english`/`arabic`/`egyptian`/`msa`/`saudi`). `match`
handles synonyms (uk→british, lady→female, soft→warm, guy→male, etc.).

## Interrupt troubleshooting

`silence` and Ctrl+Alt+S both POST `/cancel` to the daemon, which sets a cancel
flag that the playback thread acts on (the stop must run on the thread that owns
the audio device — a cross-thread stop silently fails). If an interrupt does
nothing: confirm `hooks/daemon.state` exists and the daemon is the current build
— **the daemon loads code once at spawn**, so after editing `daemon.py` (or
changing `silence_hotkey`) you must restart it (`POST /shutdown`, then it respawns
on next SessionStart, or run `hooks/session_start.ps1`).

## Project-fit suggestion

When joining a project with no pinned `speech.voice`, you may glance at its
`README.md` / `CLAUDE.md` and suggest a fitting voice early (calm narrator for
deep-research repos, expressive female for playground/creative repos) via
`match "<vibe>"`. Suggest — don't apply without asking.
