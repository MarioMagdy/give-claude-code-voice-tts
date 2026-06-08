"""Voice listing / preview / switch / random helper for the speech sandbox.

Usage:
    python hooks/voices.py list                       # show curated voices
    python hooks/voices.py current                    # show resolved voice + provenance
    python hooks/voices.py preview <voice-id> [text]  # synth + play a sample
    python hooks/voices.py sample [--lang en|ar|all]  # preview ALL in a group
    python hooks/voices.py match "<vibe>"             # find best voice by vibe (e.g. "calm british male")
    python hooks/voices.py set <voice-id>             # ephemeral: write .claude/session-state.json (this session only)
    python hooks/voices.py set <voice-id> --pin       # permanent: write .claude/settings.json (sticks across sessions)
    python hooks/voices.py enable [--pin]             # turn spoken output on  (ephemeral, or --pin to settings.json)
    python hooks/voices.py disable [--pin]            # turn spoken output off (ephemeral, or --pin to settings.json)
    python hooks/voices.py session-random             # roll a random English voice, write session-state (called by SessionStart hook)

Precedence the daemon resolves:
    .claude/session-state.json (highest)
        > project .claude/settings.json   speech.voice
        > user    ~/.claude/settings.json speech.voice
        > hardcoded DEFAULTS              speech.voice
"""
import argparse
import asyncio
import ctypes
import json
import random
import re
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent          # this repo's hooks/ — holds daemon.state (the singleton)
# Project-scoped config (session-state, project settings) targets the CURRENT
# working directory, NOT this script's repo — so voices.py operates on whatever
# project Claude is running in when installed globally. `--project DIR` (and the
# cwd default) are applied in main(); these module-level values are just the
# default and what the tests monkeypatch.
PROJECT_DIR = Path.cwd()
PROJECT_SETTINGS = PROJECT_DIR / ".claude" / "settings.json"
SESSION_STATE = PROJECT_DIR / ".claude" / "session-state.json"
USER_SETTINGS = Path.home() / ".claude" / "settings.json"


def _is_local_path(p) -> bool:
    r"""False for UNC paths and empty paths. Windows normalizes ANY two leading
    path separators -- \\, //, and the mixed forms /\ and \/ -- to a UNC path,
    so a non-local --project would make us read <project>/.claude/settings.json
    over SMB (NTLM hash leak). Mirrors the daemon/hook_worker guard."""
    s = str(p) if p else ""
    if not s:
        return False
    if len(s) >= 2 and s[0] in ("\\", "/") and s[1] in ("\\", "/"):
        return False
    return True


def _set_project_dir(project: Path):
    """Repoint the project-scoped config files at `project`. Called from main()
    for the cwd default / --project override."""
    global PROJECT_DIR, PROJECT_SETTINGS, SESSION_STATE
    PROJECT_DIR = Path(project)
    PROJECT_SETTINGS = PROJECT_DIR / ".claude" / "settings.json"
    SESSION_STATE = PROJECT_DIR / ".claude" / "session-state.json"


# ---------------------------------------------------------------------------
# Curated catalogue. Tags drive both `match` and `session-random`.
# ---------------------------------------------------------------------------

CURATED = [
    # (id, gender, language-group, label, description, sample text, tag set)
    ("en-US-AvaMultilingualNeural",     "F", "multi",
     "Ava (multilingual, US)",
     "Expressive female. Speaks 30+ languages including Arabic with a US-leaning accent.",
     "I am Ava. I can speak English and Arabic in the same voice. وأقدر أتكلم عربي كمان.",
     {"expressive", "female", "american", "us", "multilingual", "default", "warm"}),
    ("en-US-EmmaMultilingualNeural",    "F", "multi",
     "Emma (multilingual, US)",
     "Warm female. Multilingual; the upstream edge-tts default.",
     "I am Emma. Warmer than Ava, also multilingual.",
     {"warm", "female", "american", "us", "multilingual", "soft"}),
    ("en-US-AndrewMultilingualNeural",  "M", "multi",
     "Andrew (multilingual, US)",
     "Calm male. Multilingual; good for long-form listening.",
     "I am Andrew. Calm male voice, also multilingual.",
     {"calm", "male", "american", "us", "multilingual", "narrator", "measured"}),
    ("en-US-BrianMultilingualNeural",   "M", "multi",
     "Brian (multilingual, US)",
     "Friendly male. Multilingual; a bit more upbeat than Andrew.",
     "I am Brian. Friendly male, also multilingual.",
     {"friendly", "male", "american", "us", "multilingual", "upbeat"}),
    ("en-US-AvaNeural",                 "F", "en",
     "Ava (English only, US)",
     "Same Ava voice but English-only and slightly more expressive prosody.",
     "I am Ava — the English-only variant. Slightly different prosody from Ava Multilingual.",
     {"expressive", "female", "american", "us"}),
    ("en-US-ChristopherNeural",         "M", "en",
     "Christopher (US)",
     "Neutral male, US accent. Less inflected — easy for deep work.",
     "I am Christopher. Neutral male, US accent. Calm and clear.",
     {"neutral", "male", "american", "us", "narrator", "calm", "focused"}),
    ("en-GB-RyanNeural",                "M", "en",
     "Ryan (UK)",
     "Calm British male. Often picked as the least-robotic for long sessions.",
     "I am Ryan. British male voice, calm and clear.",
     {"calm", "male", "british", "uk", "least-robotic", "measured"}),
    ("en-GB-SoniaNeural",               "F", "en",
     "Sonia (UK)",
     "Warm British female.",
     "I am Sonia. British female voice with a warm tone.",
     {"warm", "female", "british", "uk"}),
    ("en-AU-NatashaNeural",             "F", "en",
     "Natasha (AU)",
     "Australian female. A different register from US/UK if you want variety.",
     "G'day, I am Natasha. Australian female voice.",
     {"female", "australian", "au"}),
    ("ar-EG-SalmaNeural",               "F", "ar",
     "Salma (Egyptian Arabic)",
     "Native Egyptian female. Best for colloquial Egyptian Arabic.",
     "أنا سلمى. صوت مصري نسائي طبيعي.",
     {"female", "egyptian", "arabic", "colloquial"}),
    ("ar-EG-ShakirNeural",              "M", "ar",
     "Shakir (Egyptian Arabic)",
     "Native Egyptian male. Calmer than Salma.",
     "أنا شاكر. صوت مصري رجالي هادي.",
     {"male", "egyptian", "arabic", "colloquial", "calm"}),
    ("ar-SA-ZariyahNeural",             "F", "ar",
     "Zariyah (MSA, Saudi)",
     "Modern Standard Arabic female. Formal, news-anchor-style.",
     "أنا زاريا، صوت بالعربية الفصحى.",
     {"female", "saudi", "arabic", "msa", "formal"}),
    ("ar-SA-HamedNeural",               "M", "ar",
     "Hamed (MSA, Saudi)",
     "Modern Standard Arabic male. Formal.",
     "أنا حامد، صوت رجالي بالعربية الفصحى.",
     {"male", "saudi", "arabic", "msa", "formal"}),
]
CURATED_BY_ID = {v[0]: v for v in CURATED}

# Pool for the session-random roll. English-friendly only, no Arabic
# (user said most discussion is in English; Arabic voices would be jarring
# for English replies). Multilingual variants stay in so the occasional
# Arabic phrase still renders ok.
RANDOM_POOL = [v[0] for v in CURATED if v[2] in ("multi", "en")]


# ---------------------------------------------------------------------------
# Settings I/O
# ---------------------------------------------------------------------------

def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _resolve_voice():
    """Returns (voice, provenance) — same precedence the daemon uses."""
    # 1. session-state (project-scoped, ephemeral)
    s = _read_json(SESSION_STATE)
    if isinstance(s.get("voice"), str):
        return s["voice"], f"session-state ({SESSION_STATE.name})"
    # 2. project settings
    p = (_read_json(PROJECT_SETTINGS).get("speech") or {})
    if isinstance(p.get("voice"), str):
        return p["voice"], "project settings.json"
    # 3. user settings
    u = (_read_json(USER_SETTINGS).get("speech") or {})
    if isinstance(u.get("voice"), str):
        return u["voice"], "user ~/.claude/settings.json"
    # 4. default
    return "en-US-AvaMultilingualNeural", "hardcoded default"


def _resolve_enabled():
    """Returns (enabled_bool, provenance) — same precedence the daemon uses
    for the `enabled` flag (session-state > project > user > default)."""
    s = _read_json(SESSION_STATE)
    if isinstance(s.get("enabled"), bool):
        return s["enabled"], f"session-state ({SESSION_STATE.name})"
    p = (_read_json(PROJECT_SETTINGS).get("speech") or {})
    if isinstance(p.get("enabled"), bool):
        return p["enabled"], "project settings.json"
    u = (_read_json(USER_SETTINGS).get("speech") or {})
    if isinstance(u.get("enabled"), bool):
        return u["enabled"], "user ~/.claude/settings.json"
    return False, "hardcoded default (off; opt-in per project)"


# ---------------------------------------------------------------------------
# Sub-command implementations
# ---------------------------------------------------------------------------

def cmd_list(args):
    by_lang = {"multi": [], "en": [], "ar": []}
    for v in CURATED:
        by_lang.setdefault(v[2], []).append(v)
    labels = {"multi": "Multilingual", "en": "English", "ar": "Arabic"}
    for lang in ("multi", "en", "ar"):
        rows = by_lang.get(lang, [])
        if not rows:
            continue
        print(f"\n{labels[lang]}:")
        for vid, gender, _, label, desc, _, tags in rows:
            print(f"  {vid:42} [{gender}]  {label}")
            print(f"  {'':42}      {desc}")
            print(f"  {'':42}      tags: {', '.join(sorted(tags))}")
    print("\nFull catalogue: python -m edge_tts --list-voices")


def cmd_current(args):
    enabled, en_prov = _resolve_enabled()
    voice, provenance = _resolve_voice()
    if getattr(args, "json", False):
        print(json.dumps({"enabled": enabled, "voice": voice}))
        return
    print(f"speech: {'on' if enabled else 'OFF'}  (source: {en_prov})")
    print(f"{voice}")
    print(f"  source: {provenance}")
    if voice in CURATED_BY_ID:
        meta = CURATED_BY_ID[voice]
        print(f"  → {meta[3]} — {meta[4]}")


def _synth_and_play(voice, text):
    import edge_tts
    out = Path(tempfile.gettempdir()) / f"voice-preview-{int(time.time()*1000)}.mp3"
    t0 = time.time()
    asyncio.run(edge_tts.Communicate(text, voice).save(str(out)))
    print(f"  synth {time.time()-t0:.2f}s ({out.stat().st_size} bytes)")
    alias = f"vp{int(time.time()*1000)%100000}"
    cmd = ctypes.windll.winmm.mciSendStringW
    buf = ctypes.create_unicode_buffer(256)
    if cmd(f'open "{out}" type mpegvideo alias {alias}', buf, 0, 0) != 0:
        print("  [mci open failed]")
        return
    try:
        cmd(f"play {alias} wait", buf, 0, 0)
    finally:
        cmd(f"close {alias}", buf, 0, 0)
        try:
            out.unlink()
        except OSError:
            pass


def cmd_preview(args):
    voice = args.voice
    text = args.text
    if voice in CURATED_BY_ID and not text:
        text = CURATED_BY_ID[voice][5]
    elif not text:
        text = "This is a test of the voice you selected."
    print(f">> {voice}")
    _synth_and_play(voice, text)


def cmd_sample(args):
    voices = CURATED
    if args.lang in ("en", "ar", "multi"):
        voices = [v for v in CURATED if v[2] == args.lang]
    if not voices:
        print(f"No curated voices for language '{args.lang}'", file=sys.stderr)
        sys.exit(1)
    for vid, _, _, label, _, sample, _ in voices:
        print(f">> {vid}  ({label})")
        _synth_and_play(vid, sample)
        time.sleep(0.5)


def _tokenize_vibe(s: str):
    """Lowercase, split on non-alpha, drop tiny tokens. Maps common synonyms."""
    SYN = {
        "uk": "british", "england": "british", "english-accent": "british",
        "us": "american", "usa": "american",
        "aussie": "australian",
        "guy": "male", "man": "male",
        "lady": "female", "woman": "female",
        "soft": "warm", "gentle": "warm",
        "deep-work": "focused", "focus": "focused",
        "lively": "expressive", "energetic": "expressive",
        "boring": "neutral", "flat": "neutral",
        "story": "narrator", "reading": "narrator",
    }
    raw = re.split(r"[^a-z]+", s.lower())
    out = []
    for t in raw:
        if len(t) < 2:
            continue
        out.append(SYN.get(t, t))
    return out


def cmd_match(args):
    tokens = _tokenize_vibe(args.vibe)
    if not tokens:
        print("No usable vibe tokens", file=sys.stderr)
        sys.exit(1)
    # Score each voice by tag-overlap; tie-break by curated order.
    scored = []
    for vid, _, _, label, desc, _, tags in CURATED:
        score = sum(1 for t in tokens if t in tags)
        if score > 0:
            scored.append((score, vid, label, desc, tags))
    scored.sort(key=lambda x: -x[0])
    if not scored:
        print(f"No curated voice matched tokens: {tokens}", file=sys.stderr)
        print("Tags available:", sorted({t for v in CURATED for t in v[6]}))
        sys.exit(1)
    print(f"vibe: '{args.vibe}'  →  tokens: {tokens}")
    print(f"top matches:")
    for score, vid, label, desc, tags in scored[:3]:
        matched = sorted(set(tokens) & tags)
        print(f"  [{score}] {vid:42} {label}")
        print(f"      matched: {', '.join(matched)}")
    best = scored[0][1]
    print(f"\nbest pick: {best}")
    if args.set:
        if args.pin:
            _set_pinned(best)
        else:
            _set_session(best)


def _set_session(voice):
    state = _read_json(SESSION_STATE)
    state["voice"] = voice
    state["picked_at"] = time.time()
    _write_json(SESSION_STATE, state)
    print(f"session-state.voice  →  {voice}  (this session only; restart re-rolls)")


def _set_pinned(voice):
    data = _read_json(PROJECT_SETTINGS)
    speech = data.setdefault("speech", {})
    old = speech.get("voice")
    speech["voice"] = voice
    _write_json(PROJECT_SETTINGS, data)
    # Clear session-state so the pinned value actually wins.
    if SESSION_STATE.exists():
        s = _read_json(SESSION_STATE)
        s.pop("voice", None)
        if s:
            _write_json(SESSION_STATE, s)
        else:
            try: SESSION_STATE.unlink()
            except OSError: pass
    print(f"settings.json speech.voice: {old}  →  {voice}")
    print("(session-state voice override cleared; pin sticks across all future sessions)")


def cmd_set(args):
    voice = args.voice
    if not re.match(r"^[a-z]{2}-[A-Z]{2}-[A-Za-z]+Neural$", voice):
        print(f"warning: '{voice}' doesn't look like an edge-tts voice id "
              f"(expected e.g. en-US-AvaNeural). Setting anyway.", file=sys.stderr)
    if args.pin:
        _set_pinned(voice)
    else:
        _set_session(voice)


def _set_enabled_session(enabled: bool):
    state = _read_json(SESSION_STATE)
    state["enabled"] = enabled
    state["picked_at"] = time.time()
    _write_json(SESSION_STATE, state)
    word = "on" if enabled else "off"
    print(f"session-state.enabled  →  {enabled}  (speech {word} this session only; restart re-rolls)")


def _set_enabled_pinned(enabled: bool):
    data = _read_json(PROJECT_SETTINGS)
    speech = data.setdefault("speech", {})
    old = speech.get("enabled")
    speech["enabled"] = enabled
    _write_json(PROJECT_SETTINGS, data)
    # Clear the session-state override so the pinned value actually wins.
    if SESSION_STATE.exists():
        s = _read_json(SESSION_STATE)
        s.pop("enabled", None)
        if s:
            _write_json(SESSION_STATE, s)
        else:
            try: SESSION_STATE.unlink()
            except OSError: pass
    print(f"settings.json speech.enabled: {old}  →  {enabled}")
    print("(session-state enabled override cleared; pin sticks across all future sessions)")


def _set_enabled_global(enabled: bool):
    data = _read_json(USER_SETTINGS)
    speech = data.setdefault("speech", {})
    old = speech.get("enabled")
    speech["enabled"] = enabled
    _write_json(USER_SETTINGS, data)
    print(f"~/.claude/settings.json speech.enabled: {old}  →  {enabled}  (global default for all projects)")


def cmd_enable(args):
    if getattr(args, "glob", False):
        _set_enabled_global(True)
    elif getattr(args, "pin", False):
        _set_enabled_pinned(True)
    else:
        _set_enabled_session(True)


def cmd_disable(args):
    if getattr(args, "glob", False):
        _set_enabled_global(False)
    elif getattr(args, "pin", False):
        _set_enabled_pinned(False)
    else:
        _set_enabled_session(False)


def cmd_silence(args):
    """Tell the daemon to stop current playback and drain the queue.
    No-op if the daemon isn't running."""
    import urllib.error
    import urllib.request
    state_file = HERE / "daemon.state"
    if not state_file.exists():
        print("daemon not running — nothing to silence", file=sys.stderr)
        sys.exit(0)
    try:
        info = json.loads(state_file.read_text(encoding="utf-8"))
        port = int(info.get("port", 0))
        token = str(info.get("token", ""))
    except Exception as e:
        print(f"could not read daemon.state: {e}", file=sys.stderr)
        sys.exit(0)
    if port <= 0 or not token:
        print("daemon.state missing port or token", file=sys.stderr)
        sys.exit(0)
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/cancel",
            data=b"",
            headers={"X-Speech-Token": token},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=2) as resp:
            body = resp.read().decode("utf-8")
            print(body)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"daemon unreachable: {e}", file=sys.stderr)
        sys.exit(0)


def cmd_session_random(args):
    # Only roll where speech is enabled for this project — otherwise we'd write
    # a session-state.json into every project the user merely opens (off ones
    # included), littering arbitrary repos. Off → no-op.
    if not _resolve_enabled()[0]:
        print("speech disabled for this project; skipping voice roll")
        return
    # Avoid picking the same one as last time, if we remember it.
    state = _read_json(SESSION_STATE)
    prev = state.get("voice")
    pool = [v for v in RANDOM_POOL if v != prev] or list(RANDOM_POOL)
    pick = random.choice(pool)
    state["voice"] = pick
    state["picked_at"] = time.time()
    state["picked_by"] = "session-random"
    # SC6 — an ephemeral `enable` from a prior session must NOT bleed into this
    # one. Drop any stale `enabled` override so the new session reverts to the
    # pinned project setting (or off if none). Voice / picked_at / picked_by
    # are session-local and stay.
    state.pop("enabled", None)
    _write_json(SESSION_STATE, state)
    label = CURATED_BY_ID[pick][3] if pick in CURATED_BY_ID else pick
    print(f"session voice rolled: {pick}  ({label})")


def main():
    # Force UTF-8 stdout/stderr so the Unicode in voice labels (→, —, Arabic
    # samples, etc.) prints cleanly on Windows where the default is cp1252.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    p = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    p.add_argument("--project", default=None,
                   help="project dir for session-state/settings (default: cwd). "
                        "Used by hooks that know the project from their payload.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list",    help="show curated voices").set_defaults(func=cmd_list)
    cu = sub.add_parser("current", help="show resolved voice + where it came from")
    cu.add_argument("--json", action="store_true", help="emit {\"enabled\":bool,\"voice\":str} for hooks")
    cu.set_defaults(func=cmd_current)

    pv = sub.add_parser("preview", help="synth + play a sample for one voice")
    pv.add_argument("voice")
    pv.add_argument("text", nargs="?", default=None)
    pv.set_defaults(func=cmd_preview)

    sm = sub.add_parser("sample", help="preview ALL curated voices in a language group")
    sm.add_argument("--lang", choices=["en", "ar", "multi", "all"], default="all")
    sm.set_defaults(func=cmd_sample)

    mt = sub.add_parser("match", help="pick a voice by vibe (e.g. 'calm british male')")
    mt.add_argument("vibe")
    mt.add_argument("--set", action="store_true", help="apply the best pick (ephemeral by default)")
    mt.add_argument("--pin", action="store_true", help="when used with --set, pin permanently to settings.json")
    mt.set_defaults(func=cmd_match)

    st = sub.add_parser("set", help="set the active voice (ephemeral by default; --pin for permanent)")
    st.add_argument("voice")
    st.add_argument("--pin", action="store_true", help="write to project settings.json instead of session-state")
    st.set_defaults(func=cmd_set)

    sr = sub.add_parser("session-random", help="pick a random English-friendly voice into session-state (called by SessionStart)")
    sr.set_defaults(func=cmd_session_random)

    en = sub.add_parser("enable", help="turn spoken output on (ephemeral; --pin project; --global all projects)")
    en.add_argument("--pin", action="store_true", help="write speech.enabled=true to project settings.json")
    en.add_argument("--global", dest="glob", action="store_true", help="write speech.enabled=true to ~/.claude/settings.json (global default)")
    en.set_defaults(func=cmd_enable)

    di = sub.add_parser("disable", help="turn spoken output off (ephemeral; --pin project; --global all projects)")
    di.add_argument("--pin", action="store_true", help="write speech.enabled=false to project settings.json")
    di.add_argument("--global", dest="glob", action="store_true", help="write speech.enabled=false to ~/.claude/settings.json (global default)")
    di.set_defaults(func=cmd_disable)

    sl = sub.add_parser("silence", help="stop current playback + drain queued jobs")
    sl.set_defaults(func=cmd_silence)

    args = p.parse_args()
    # SC1 — only repoint at --project when it is a local path; a UNC/mixed-slash
    # project dir would make the project-settings read hit an SMB share.
    if args.project and _is_local_path(args.project):
        _set_project_dir(Path(args.project))
    args.func(args)


if __name__ == "__main__":
    main()
