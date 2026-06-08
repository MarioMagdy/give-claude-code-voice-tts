# give-claude-code-voice-tts — give Claude Code a spoken voice on Windows

> **give-claude-code-voice-tts** is a Claude Code plugin that gives Claude Code **text-to-speech** on
> Windows: a **Stop hook** plus a persistent **edge-tts daemon** read a short `<spoken>` summary of
> each reply aloud, with a **`speech` skill** to switch voices, mute, and interrupt. Off by default,
> opt-in per project.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Claude Code Plugin](https://img.shields.io/badge/Claude%20Code-plugin-7c3aed.svg)](https://code.claude.com/docs/en/plugins)
[![Platform: Windows](https://img.shields.io/badge/platform-Windows-0078d6.svg)](#requirements)

**Last verified:** June 2026.

Claude ends conversational replies with a one-line `<spoken>…</spoken>` summary; the hook speaks
**just that** — never your code, tables, or file dumps. TTS is free neural **edge-tts** (Microsoft
Edge voices); there's no cloud account, no API key, and no per-character billing.

## Install

```text
/plugin marketplace add MarioMagdy/give-claude-code-voice-tts
/plugin install voice-tts@give-claude-code-voice-tts
```

It's **off by default everywhere**. Turn it on for the current project:

```text
/speech        # opens the control menu — choose "Turn speech on"
```

(or run `python "$CLAUDE_PLUGIN_ROOT/hooks/voices.py" enable --pin`). New sessions then speak.

## When to use this

Install this if you want Claude Code to:

- **Read its replies aloud** while you keep your eyes on the code
- Speak a **spoken summary** after each response (the Stop-hook pattern)
- Control the voice by command — switch voices, change the vibe, mute, or interrupt mid-sentence

Trigger phrases: *"make Claude talk"*, *"turn on TTS"*, *"give Claude a voice"*, *"stop speaking"*,
*"mute"*, *"speak again"*, *"switch to a calmer/British/male voice"*.

## How it works

1. A SessionStart hook injects the `<spoken>` emission rule (only where speech is enabled) and
   ensures the daemon is running.
2. Claude ends each conversational reply with `<spoken>…</spoken>` (plain prose, no markdown).
3. The **Stop hook** (`hook_post.py`) POSTs the reply to a warm localhost **edge-tts daemon**
   (`daemon.py`), which synthesises and plays it via Windows audio (MCI).
4. The daemon stays warm so there's no per-reply Python/import/TLS cold-start — roughly 2–3× faster
   than spawning per utterance, and it serialises audio so overlapping sessions never talk over
   each other.

The daemon is **localhost-only and token-authenticated** (the token lives in a gitignored
`daemon.state`), bounds request size, and rejects non-local file paths.

## Control — the `speech` skill

Say things, or use the menu (`/speech`):

| You say | What happens |
|---|---|
| "turn speech on/off", "mute", "stop speaking" | enable / disable spoken output |
| "stop talking right now" (or `Ctrl+Alt+S`) | interrupt the audio currently playing |
| "switch to Ryan", "use a calmer / more British / male voice" | change the voice (by id or by vibe) |
| "list / preview / sample voices" | browse the curated voice set |

Voices are Microsoft Edge neural voices (e.g. `en-GB-RyanNeural`, `en-US-AvaMultilingualNeural`,
plus Arabic options). Changes apply on the next reply — no restart.

## Comparison

| Method | Speaks each reply automatically | Reads summaries only (not code) | Voice controls | API key / account |
|---|---|---|---|---|
| **give-claude-code-voice-tts** | Yes | Yes | switch / mute / interrupt | No |
| Direct `edge-tts` script | No (manual) | No | No | No |
| Windows Narrator / SAPI | No | No | Limited | No |

## Requirements

- **Windows 10/11** (playback uses MCI via `winmm.dll`)
- **Python 3.10+** on PATH (with `pythonw.exe`)
- `pip install edge-tts` (free neural TTS; no account)
- Optional: `pip install keyboard` for the `Ctrl+Alt+S` global interrupt hotkey

## FAQ

**What is give-claude-code-voice-tts?**
A Claude Code plugin that gives Claude Code text-to-speech on Windows: a Stop hook plus a persistent edge-tts daemon read a short spoken summary of each reply aloud, with a `speech` skill to switch voices, mute, and interrupt. Off by default, opt-in per project.

**Does this require an API key or a paid TTS service?**
No. It uses `edge-tts` (Microsoft Edge's neural voices), which is free. No account, no API key.

**Does it work on macOS or Linux?**
Not yet — playback is Windows-only (MCI/`winmm.dll`). macOS/Linux support is a possible future
addition (the synth step is cross-platform; only playback is Windows-specific).

**Will it read code and tables aloud?**
No. Claude only puts a short plain-prose summary inside `<spoken>`; data-shaped replies get no tag,
and a sanitiser strips stray markdown as a safety net.

**How do I stop it mid-sentence?**
Say "stop talking right now", press `Ctrl+Alt+S`, or run the skill's `silence` command.

**Is it on by default?**
No — it's off everywhere until you opt a project in. Zero blast radius in repos you didn't enable.

**How do I give Claude Code a voice?**
Install the plugin (`/plugin marketplace add MarioMagdy/give-claude-code-voice-tts` then
`/plugin install voice-tts@give-claude-code-voice-tts`) and run `/speech` to open the
control menu, or directly: `python "$CLAUDE_PLUGIN_ROOT/hooks/voices.py" enable --pin`. After
that, Claude ends conversational replies with a short `<spoken>` summary which the Stop hook
speaks aloud.

**How do I make Claude Code talk?**
Same answer — install the plugin, then `enable` (or pick "Turn speech on" from the `/speech`
menu). You'll hear a one-line spoken summary at the end of each conversational reply; data-
shaped replies (tables, code blocks, file dumps) are silent by design.

**Is this useful for accessibility?**
Yes — spoken replies let you keep your eyes off the terminal. Useful for low-vision users,
ADHD focus/momentum (audio confirmation that a long task finished), or multitasking while
your hands stay on the keyboard. Use `Ctrl+Alt+S` (or the `silence` skill command) to cut off
audio mid-sentence.

## What's inside

```
.claude-plugin/plugin.json     ← plugin manifest (declares the hooks)
hooks/
  hooks.json                   ← SessionStart / Stop / SessionEnd wiring
  daemon.py                    ← persistent edge-tts synth + playback (token-authed, localhost)
  hook_post.py                 ← Stop hook: POST the reply to the daemon
  session_start.ps1            ← ensure daemon + roll a session voice (when enabled)
  speech_status.ps1            ← inject the <spoken> rule + enabled-state (when enabled)
  session_end.ps1              ← stop audio on exit
  voices.py                    ← the control engine (enable/disable/set/match/silence)
skills/speech/SKILL.md         ← the voice-control skill
CONTINGENCIES.md               ← pre-decided fallbacks for known failure modes
```

## License

[MIT](LICENSE) © MarioMagdy
