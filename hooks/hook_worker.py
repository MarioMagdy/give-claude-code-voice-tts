"""TTS worker for the Claude Code Stop hook. Runs detached under pythonw.exe.

Reads the Stop-hook JSON payload from a temp file (path = argv[1]), finds the
last non-sidechain assistant turn in the transcript, greps a <spoken>...</spoken>
tag out of its text, synthesises via edge_tts, and plays via Windows MCI.

Everything here is best-effort: a TTS failure must never surface to the user.
"""
import asyncio
import ctypes
import json
import os
import re
import sys
import tempfile
from pathlib import Path

GLOBAL_SETTINGS = Path.home() / ".claude" / "settings.json"
DEFAULTS = {
    "enabled": False,
    "voice": "en-US-AvaNeural",
    "rate": "+0%",
    "volume": "+0%",
}
SPOKEN_RE = re.compile(r"<spoken>(.*?)</spoken>", re.DOTALL | re.IGNORECASE)


def _read_speech_block(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        block = data.get("speech")
        return block if isinstance(block, dict) else {}
    except Exception:
        return {}


def _read_session_state(cwd: str) -> dict:
    """Highest-precedence layer: ephemeral session-state file. Pull only the
    speech-relevant keys (voice/rate/volume/enabled)."""
    if not cwd:
        return {}
    p = Path(cwd) / ".claude" / "session-state.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        return {k: data[k] for k in ("voice", "rate", "volume", "enabled") if k in data}
    except Exception:
        return {}


def _load_config(cwd):
    # Precedence: session-state > project settings > global settings > defaults(off).
    # Session-state is applied LAST so it can override pinned project values too
    # (matches what daemon.py does).
    cfg = dict(DEFAULTS)
    cfg.update(_read_speech_block(GLOBAL_SETTINGS))
    if cwd:
        cfg.update(_read_speech_block(Path(cwd) / ".claude" / "settings.json"))
        cfg.update(_read_session_state(cwd))
    return cfg


def _read_payload(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _last_assistant_text(transcript_path):
    """Scan the JSONL, return concatenated text blocks of the last text-bearing
    non-sidechain assistant turn. Empty string if none found."""
    last_text = ""
    with open(transcript_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if obj.get("isSidechain"):
                continue
            if obj.get("type") != "assistant":
                continue
            content = (obj.get("message") or {}).get("content") or []
            if not isinstance(content, list):
                continue
            chunks = [
                b.get("text", "")
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            if chunks:
                last_text = "\n".join(chunks)
    return last_text


async def _synth(text, cfg, out_path):
    import edge_tts  # imported lazily so a missing dep doesn't kill the worker silently elsewhere
    await edge_tts.Communicate(
        text, cfg["voice"], rate=cfg["rate"], volume=cfg["volume"]
    ).save(str(out_path))


def _play_mci(mp3):
    """winmm.dll MCI playback. Stdlib-only, no window, blocks until done."""
    alias = f"claudetts{os.getpid()}"
    cmd = ctypes.windll.winmm.mciSendStringW
    buf = ctypes.create_unicode_buffer(256)
    if cmd(f'open "{mp3}" type mpegvideo alias {alias}', buf, 0, 0) != 0:
        return
    try:
        cmd(f"play {alias} wait", buf, 0, 0)
    finally:
        cmd(f"close {alias}", buf, 0, 0)


def main():
    if len(sys.argv) < 2:
        return
    payload = _read_payload(sys.argv[1])
    transcript = payload.get("transcript_path")
    if not transcript or not os.path.exists(transcript):
        return

    cfg = _load_config(payload.get("cwd"))
    if not cfg.get("enabled", True):
        return   # master toggle off -> silent

    text = _last_assistant_text(transcript)
    if not text:
        return
    matches = SPOKEN_RE.findall(text)
    if not matches:
        return                # no tag -> no audio (silent by design)
    spoken = matches[-1].strip()
    if not spoken:
        return

    mp3 = Path(tempfile.gettempdir()) / f"claude-tts-{os.getpid()}.mp3"
    try:
        asyncio.run(_synth(spoken, cfg, mp3))
        _play_mci(mp3)
    except Exception:
        pass                  # never let TTS failure surface
    finally:
        try:
            mp3.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    main()
