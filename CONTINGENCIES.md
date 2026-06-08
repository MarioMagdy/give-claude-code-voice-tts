# Contingencies — claude-tools / speech

When something breaks, look here first. Each scenario has a pre-decided
fallback so we don't have to invent one mid-incident.

## C1. edge-tts stops working (Azure blocked, rate-limited, API changed)

**Symptom:** Worker runs without errors but no audio; or
`edge_tts.Communicate(...).save()` raises a network/auth error in logs.

**Fix tier 1 — try a different voice.** Some voices get deprecated; swap
`speech.voice` to a known-stable one (`en-US-EmmaMultilingualNeural` is the
upstream default and tends to outlive others).

**Fix tier 2 — pin edge-tts version.**
`pip install edge-tts==<last-known-good-version>`. Check the
rany2/edge-tts GitHub issues for the latest working pin; this has happened
before (see issue #305 family).

**Fix tier 3 — swap engine to Windows SAPI (offline, built-in).** Replace
`_synth` + `_play_mci` in `hook_worker.py` with one PowerShell call that reads
the spoken text from STDIN (NEVER interpolate `text` into the -Command
string — backslashes and PowerShell quote escaping are not safe):

```python
import subprocess
def _speak_sapi(text: str, rate: int = 0):
    ps = ("$t=[Console]::In.ReadToEnd();"
          "Add-Type -AssemblyName System.Speech;"
          "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
          f"$s.Rate={rate};$s.Speak($t)")
    subprocess.run(["powershell","-NoProfile","-Command",ps],
                   input=text, text=True, creationflags=0x08000000)
```

Robotic but online-independent. Drop edge-tts dep entirely if migrating
long-term.

**Fix tier 4 — switch to Piper TTS (offline neural).** `pip install piper-tts`,
download a voice model, swap `_synth`. Closer to neural quality, fully
offline, ~50MB model file. Document the chosen voice in the repo so
reproducible.

## C2. Audio latency still feels too long (>1.5 s before first sound)

The persistent daemon (now the default architecture) already eliminated
~700 ms-1 s of per-utterance spawn cost. Realistic baseline post-daemon:
~1 s from Claude's last token to first audio byte. If it's worse:

**Diagnose first — is the daemon actually running?** Open the sandbox
repo, check `hooks/daemon.state`. If missing, SessionStart didn't fire —
restart Claude Code or run `hooks/session_start.ps1` manually and confirm
a state file appears within 3 s. If the daemon is up but hooks still spawn
Python from cold every turn, the Stop hook in `.claude/settings.json` is
probably still pointing at `hook_entry.ps1` (PowerShell, ~1.3 s) or
`hook_worker.py` (full direct synth, ~3 s). It should point at
`hook_post.py`.

**Fix tier 1 — pre-warm Azure on first hook.** The daemon's lazy-loaded
`edge_tts` finishes importing on a background `_prewarm` thread shortly
after SessionStart. The FIRST synth of a session still pays the Azure
TLS+websocket handshake (~300-600 ms). To eliminate that too, send a
silent throwaway synth from the daemon on startup. Add to `daemon.py`'s
`_prewarm` after `_ensure_edge_tts()`:

```python
try:
    out = Path(tempfile.gettempdir()) / "prewarm.mp3"
    _LOOP.call_soon_threadsafe(asyncio.ensure_future,
        _edge_tts.Communicate(" ", DEFAULTS["voice"]).save(str(out)))
except Exception:
    pass
```

**Fix tier 2 — switch to SAPI (zero-latency, see C1 tier 3)** if neural
quality isn't worth the wait for your workflow. Drops Azure dependency
entirely; speech starts in <100 ms.

**Fix tier 3 — streaming playback.** Have the daemon pipe edge_tts chunks
into a player that decodes incrementally (mpv, ffplay with `-nodisp`)
instead of saving the full mp3 first. Saves ~200-400 ms on long
utterances. Adds an external dependency and the frame-alignment gotcha
from edge-tts issue #187 — only worth it if you're regularly speaking
multi-sentence summaries.

## C3. Hook fires multiple times per turn (audio plays twice)

**Symptom:** You hear the same `<spoken>` content back-to-back.

**Likely cause:** Both project `<sandbox>/.claude/settings.json` and user
`~/.claude/settings.json` define `hooks.Stop` and you're inside the sandbox
repo. Claude Code additively merges hook arrays.

**Fix:** Remove the `hooks.Stop` entry from one of them. After Phase 2
promote, the project-scope entry should go (keep the `speech` block as a
per-project override only). The `SessionStart` entry has the same
double-fire risk — pick one home for it too.

**Defensive belt-and-braces:** the daemon already deduplicates by virtue
of the single serial job queue, but it doesn't drop *content-identical*
jobs. If needed, add a "last spoken hash + timestamp" guard to
`_process_job()` and short-circuit when a duplicate arrives within 2 s.

## C4. Spoken content sounds wrong (markdown read aloud, code spoken letter-by-letter, URLs read verbatim)

**Symptom:** TTS reads `**bold**` as "asterisk asterisk bold asterisk
asterisk", reads `https://…` letter by letter, or spells out
`function_name`.

**First line of defense:** the CLAUDE.md instruction already says not to
put markdown/code/URLs inside `<spoken>`. If the model is misbehaving,
strengthen the instruction with a worked example.

**Worker-side sanitizer (defense in depth):** add to `hook_worker.py`
before `_synth`:

```python
def _sanitize(text: str) -> str:
    import re
    text = re.sub(r"`([^`]+)`", r"\1", text)              # inline code -> plain
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)          # **bold**
    text = re.sub(r"\*(.+?)\*", r"\1", text)              # *italics*
    text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text) # [text](url) -> text
    text = re.sub(r"https?://\S+", "link", text)          # bare URLs -> "link"
    return text.strip()
```

## C5. Two Claude Code sessions open simultaneously, both speak at once

**Symptom:** Overlapping audio when running multiple terminals.

**Fix:** Acquire a named OS mutex in `_play_mci` (Windows: `CreateMutexW`
via ctypes). Whoever holds it plays; others skip the playback (synthesis
still happens but is discarded). Two lines of code; add when it becomes
annoying, not before.

## C6. Model stops emitting `<spoken>` reliably after long sessions

**Symptom:** Audio works at session start but trails off over long
conversations.

**Likely cause:** CLAUDE.md is at the start of context; once compaction
kicks in, the instruction may be deprioritized.

**Fix:** Periodically remind via a `UserPromptSubmit` hook that re-injects
the spoken-tag rule as a system message every N turns (or after compaction
events). Last-resort; usually unnecessary.

## C7. Need to speak something outside a reply (announce a long-running task finished, etc.)

**Not a failure — a feature need.** Add a tiny CLI:
`python hooks/say.py "your text"`. Bypasses Claude Code entirely. Useful
as a building block for other hooks (PostToolUse on long-running commands,
etc.).

## C8. Want to disable speech for one specific reply without disabling globally

**Not a failure — a feature need.** Tell Claude "this one, don't speak" —
the model just omits the tag. No code change needed.
