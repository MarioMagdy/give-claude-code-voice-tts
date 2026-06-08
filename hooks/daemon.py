"""Speech daemon — long-lived HTTP server that does edge-tts synthesis + MCI
playback for the Claude Code Stop hook.

Spawned once by `session_start.ps1` on SessionStart (idempotent — won't double-
spawn if already running). Holds a warm Python interpreter, pre-imported
`edge_tts`, and a running asyncio loop, eliminating ~700-1500 ms of per-utterance
spawn cost that the direct-spawn worker had.

Transport: HTTP on `127.0.0.1` on an ephemeral port. Port + PID + a random
auth token are written to `daemon.state` next to this file so the Stop hook
can find us with a single curl. Localhost-only; the action endpoints
(/stop, /cancel, /shutdown) require an `X-Speech-Token` header that matches
the token in `daemon.state` — the token never leaves the user's home dir.

Concurrency: jobs are serialised through a single bounded queue → one
playback at a time, which also solves the multi-session-overlap problem
(CONTINGENCIES C5) for free. The queue is bounded (maxsize=64) so a flood
of /stop requests can't grow memory unbounded.

Endpoints:
    POST /stop      — body is the raw Stop-hook JSON payload from Claude Code.
                      Token required. Body <= 256 KB. Returns 204 on queue
                      success, 401 on bad token, 413 on oversize body,
                      429 if the queue is full.
    POST /cancel    — token required.
    POST /shutdown  — token required.
    GET  /ping      — health check, open.
    GET  /status    — health check, open.
"""
import asyncio
import ctypes
import hmac
import json
import os
import queue
import re
import secrets
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "daemon.state"
MAX_BODY = 262144  # 256 KB cap on POST bodies (F9b)
_DAEMON_TOKEN: str = ""  # generated in main(); compared in _Handler._check_token

GLOBAL_SETTINGS = Path.home() / ".claude" / "settings.json"
DEFAULTS = {
    "enabled": False,   # OFF by default — speech is opt-in per project (global rollout)
    "voice": "en-US-AvaMultilingualNeural",
    "rate": "+0%",
    "volume": "+0%",
    "silence_hotkey": "ctrl+alt+s",
}
SPOKEN_RE = re.compile(r"<spoken>(.*?)</spoken>", re.DOTALL | re.IGNORECASE)

# Single MCI alias so /cancel can target the currently-playing clip.
# Playback is serialised through the job queue → only one clip plays at a
# time → one alias is enough.
ACTIVE_ALIAS = "claudetts_active"
_active_lock = threading.Lock()
_active_playing = False        # True between mci open and mci close
# Set by /cancel (or the hotkey) to request interruption. CRITICAL: MCI
# aliases are owned by the thread that opened them — a `stop` from any other
# thread fails with error 263 and is silently ignored. So the cancel path
# only flips this event; the owning playback thread (_play) sees it and
# issues the actual MCI stop itself.
_cancel_event = threading.Event()


# ---------------------------------------------------------------------------
# Markdown sanitiser. Claude leaks single markdown characters into <spoken>
# blocks (a stray backtick around a code identifier, an asterisk for
# emphasis, etc.) and TTS reads them literally ("asterisk asterisk bold").
# Strip the obvious offenders before synth — defence in depth on top of the
# CLAUDE.md instruction to not include markdown in the first place.
# ---------------------------------------------------------------------------

_MD_PATTERNS = [
    (re.compile(r"```[\s\S]*?```"), " "),                    # fenced code blocks
    (re.compile(r"`([^`]+)`"),       r"\1"),                 # inline code
    (re.compile(r"^\s*#+\s+", re.M), ""),                    # # headers at line start
    (re.compile(r"\*\*(.+?)\*\*"),   r"\1"),                 # **bold**
    (re.compile(r"\*(.+?)\*"),       r"\1"),                 # *italics*
    (re.compile(r"__(.+?)__"),       r"\1"),                 # __bold__
    (re.compile(r"(?<!\w)_(.+?)_(?!\w)"), r"\1"),            # _italics_ (avoid snake_case)
    (re.compile(r"^\s*[-*+]\s+", re.M), ""),                 # bullet markers
    (re.compile(r"^\s*>\s+", re.M),  ""),                    # blockquote markers
    (re.compile(r"\[([^\]]+)\]\([^)]+\)"), r"\1"),           # [text](url) → text
    (re.compile(r"https?://\S+"),    "link"),                # bare URLs
    (re.compile(r"<([^>]+)>"),       r"\1"),                 # <tag> → tag (keep content, drop brackets)
    (re.compile(r"\s+"),             " "),                   # collapse whitespace
]


def _sanitize_for_speech(text: str) -> str:
    for pat, repl in _MD_PATTERNS:
        text = pat.sub(repl, text)
    return text.strip()

# ---------------------------------------------------------------------------
# Config + transcript helpers (same logic as hook_worker.py kept identical so
# behaviour is reproducible if we ever fall back to direct-spawn).
# ---------------------------------------------------------------------------

def _read_speech_block(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        block = data.get("speech")
        return block if isinstance(block, dict) else {}
    except Exception:
        return {}


def _read_session_state(cwd_path: Path) -> dict:
    """Highest-precedence layer: ephemeral session-state file overwritten
    by voices.py session-random / set on each SessionStart or user switch."""
    p = cwd_path / ".claude" / "session-state.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        # Pull only the speech-relevant keys.
        out = {}
        for k in ("voice", "rate", "volume", "enabled"):
            if k in data:
                out[k] = data[k]
        return out
    except Exception:
        return {}


def _is_local_path(p) -> bool:
    r"""False for UNC paths and empty paths. SC1 guard: a token-holding local
    caller sending a UNC path would cause an outbound SMB read (NTLM hash leak)
    when we read <path>/.claude/settings.json. Windows normalizes ANY two
    leading path separators -- \\, //, AND the mixed forms /\ and \/ -- to a
    UNC path, so reject every two-separator prefix, not just the literal \\
    and // ones (the mixed forms /\host\share and \/host/share would otherwise
    slip through and still resolve to \\host\share)."""
    s = str(p) if p else ""
    if not s:
        return False
    if len(s) >= 2 and s[0] in ("\\", "/") and s[1] in ("\\", "/"):
        return False
    return True


def _load_config(cwd):
    cfg = dict(DEFAULTS)
    cfg.update(_read_speech_block(GLOBAL_SETTINGS))
    # Defense in depth: if cwd is set but not local (UNC / `//`-prefixed / empty),
    # treat as None and skip the cwd reads entirely. SC1.
    if cwd and _is_local_path(cwd):
        cwd_path = Path(cwd)
        cfg.update(_read_speech_block(cwd_path / ".claude" / "settings.json"))
        cfg.update(_read_session_state(cwd_path))
    return cfg


def _last_assistant_text(transcript_path):
    last_text = ""
    try:
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
    except Exception:
        return ""
    return last_text


# ---------------------------------------------------------------------------
# Synth + playback. edge_tts is imported LAZILY (not at module load) so the
# state file appears in tens of ms instead of seconds — under pythonw.exe
# the aiohttp+websockets chain that edge_tts pulls in adds ~1.5 s of import
# cost which would otherwise block SessionStart's "is the daemon ready yet?"
# probe. A background pre-warm thread starts the import in parallel right
# after the server is listening, so by the time the first real Stop hook
# fires it's almost always done.
# ---------------------------------------------------------------------------

_edge_tts = None
_edge_tts_lock = threading.Lock()

_LOOP = asyncio.new_event_loop()
threading.Thread(target=_LOOP.run_forever, daemon=True, name="tts-asyncio").start()


def _ensure_edge_tts():
    """Import edge_tts on demand. Thread-safe; cheap after the first call."""
    global _edge_tts
    if _edge_tts is None:
        with _edge_tts_lock:
            if _edge_tts is None:
                import edge_tts as _et
                _edge_tts = _et
    return _edge_tts


def _prewarm():
    """Background pre-import so the first synth doesn't pay the import cost."""
    try:
        _ensure_edge_tts()
    except Exception:
        pass


def _synth(text, cfg, out_path):
    """Block until the mp3 is written. Runs on the persistent loop."""
    edge_tts = _ensure_edge_tts()
    fut = asyncio.run_coroutine_threadsafe(
        edge_tts.Communicate(
            text, cfg["voice"], rate=cfg["rate"], volume=cfg["volume"]
        ).save(str(out_path)),
        _LOOP,
    )
    fut.result()  # propagate exceptions to the worker thread


def _play(mp3):
    """winmm.dll MCI playback. Blocks until done/cancelled; runs on the worker
    thread.

    Every MCI command for ACTIVE_ALIAS MUST run on this one thread: MCI
    devices are owned by the thread that opened them, so a `stop` from the
    HTTP-handler or hotkey thread fails (error 263) and never interrupts
    playback. That was the interrupt bug. Therefore we DON'T use a blocking
    `play ... wait` that another thread would have to break; we issue an
    async `play` and poll here, watching _cancel_event, and WE issue the stop
    when it's set."""
    global _active_playing
    cmd = ctypes.windll.winmm.mciSendStringW
    buf = ctypes.create_unicode_buffer(256)

    def mci(s):
        # length 256 so `status ... mode` can write its result back into buf.
        return cmd(s, buf, 256, 0)

    with _active_lock:
        # Close any leftover handle (defensive — should be closed already).
        mci(f"close {ACTIVE_ALIAS}")
        if mci(f'open "{mp3}" type mpegvideo alias {ACTIVE_ALIAS}') != 0:
            return
        _cancel_event.clear()
        _active_playing = True
    try:
        if mci(f"play {ACTIVE_ALIAS}") != 0:   # async — returns immediately
            return
        # Brief grace so we don't read a premature "stopped" before the device
        # actually enters "playing".
        time.sleep(0.08)
        while not _cancel_event.is_set():
            mci(f"status {ACTIVE_ALIAS} mode")
            if buf.value != "playing":         # natural end (or paused/closed)
                break
            time.sleep(0.05)
        if _cancel_event.is_set():
            mci(f"stop {ACTIVE_ALIAS}")         # same thread that opened it → works
    finally:
        with _active_lock:
            mci(f"close {ACTIVE_ALIAS}")
            _active_playing = False


def _cancel_current() -> dict:
    """Request interruption of the in-progress clip + drain the queue. Returns
    a status dict for the /cancel response body.

    Runs on the HTTP-handler thread (/cancel) or the hotkey thread — NEITHER
    owns the MCI device, so we must not call MCI here (it would 263-fail
    silently, the original bug). We only set _cancel_event; the owning _play
    thread issues the real stop within ~50 ms and then closes the alias."""
    with _active_lock:
        stopped = _active_playing
    if stopped:
        _cancel_event.set()
    # Drain pending jobs so queued clips don't start after a cancel.
    drained = 0
    while True:
        try:
            _job_q.get_nowait()
            drained += 1
        except queue.Empty:
            break
    return {"stopped": stopped, "drained": drained}


# ---------------------------------------------------------------------------
# Job queue — serialised synth+play. One job runs at a time so multiple
# concurrent sessions never produce overlapping audio.
# ---------------------------------------------------------------------------

_job_q: "queue.Queue[dict]" = queue.Queue(maxsize=64)


LOG_FILE = Path.home() / ".claude" / "speech-daemon.log"


def _log_job(cwd, voice, status, text_snippet=""):
    """Append one line to the per-user daemon log so we have ground truth
    about which voice was used per job, regardless of whether anyone is
    listening live. Best-effort — never raises. The spoken text is NEVER
    written: only timestamp + voice + status. `cwd` and `text_snippet` are
    accepted for call-site compatibility but ignored."""
    try:
        log = LOG_FILE
        log.parent.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y-%m-%dT%H:%M:%S")
        with log.open("a", encoding="utf-8") as f:
            f.write(f"{ts}  voice={voice}  status={status}\n")
    except Exception:
        pass


def _process_job(payload):
    cwd = payload.get("cwd")
    transcript = payload.get("transcript_path")
    # F9d / SC1 — reject UNC paths and anything that isn't a local regular file
    # before any I/O. UNC like `\\host\share\file` (and the mixed-slash forms
    # `/\host` and `\/host`, which Windows also resolves to UNC) would cause an
    # outbound SMB connection on read; _is_local_path rejects all of them.
    if not transcript or not isinstance(transcript, str):
        _log_job(cwd, "?", "skip:bad-path")
        return
    if not _is_local_path(transcript):
        _log_job(cwd, "?", "skip:bad-path")
        return
    if not os.path.exists(transcript) or not os.path.isfile(transcript):
        _log_job(cwd, "?", "skip:bad-path")
        return
    # SC1 — also guard the cwd path before we read any cwd-scoped config. A
    # UNC cwd would cause an outbound SMB read on _load_config.
    cfg = _load_config(cwd if _is_local_path(cwd) else None)
    if not cfg.get("enabled", True):
        _log_job(cwd, cfg.get("voice"), "skip:disabled")
        return
    text = _last_assistant_text(transcript)
    if not text:
        _log_job(cwd, cfg.get("voice"), "skip:no-text")
        return
    matches = SPOKEN_RE.findall(text)
    if not matches:
        _log_job(cwd, cfg.get("voice"), "skip:no-tag")
        return
    spoken = matches[-1].strip()
    if not spoken:
        _log_job(cwd, cfg.get("voice"), "skip:empty-tag")
        return
    voice = cfg["voice"]
    spoken = _sanitize_for_speech(spoken)
    if not spoken:
        _log_job(cwd, voice, "skip:sanitized-empty")
        return
    mp3 = Path(tempfile.gettempdir()) / f"claude-tts-d-{os.getpid()}-{int(time.time()*1000)}.mp3"
    try:
        _synth(spoken, cfg, mp3)
        _play(mp3)
        _log_job(cwd, voice, "played", spoken)
    except Exception as e:
        _log_job(cwd, voice, f"error:{type(e).__name__}", spoken)
        raise
    finally:
        try:
            mp3.unlink()
        except OSError:
            pass


def _worker():
    while True:
        job = _job_q.get()
        try:
            _process_job(job)
        except Exception:
            # Never crash the worker; one bad job shouldn't break the daemon.
            pass


threading.Thread(target=_worker, daemon=True, name="tts-worker").start()


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    server_version = "claude-tts-daemon/1.0"

    def log_message(self, format, *args):
        # Silence default per-request stderr noise.
        return

    def _read_body(self):
        """Read request body, enforcing a hard cap of MAX_BODY bytes. On
        oversize, respond 413 immediately and consume nothing more from the
        stream."""
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            length = 0
        if length < 0:
            length = 0
        if length > MAX_BODY:
            self.send_response(413)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        return self.rfile.read(length) if length > 0 else b""

    def _check_token(self) -> bool:
        """Constant-time token check. False → 401 already sent."""
        presented = self.headers.get("X-Speech-Token", "")
        if not _DAEMON_TOKEN:
            return False
        ok = hmac.compare_digest(presented.encode("utf-8"), _DAEMON_TOKEN.encode("utf-8"))
        if not ok:
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
        return ok

    def do_GET(self):
        if self.path == "/ping":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")
        elif self.path == "/status":
            with _active_lock:
                playing = _active_playing
            body = json.dumps({"playing": playing}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/stop":
            if not self._check_token():
                return
            body = self._read_body()
            if body is None:
                return  # 413 already sent
            try:
                payload = json.loads(body.decode("utf-8", errors="replace")) if body else {}
            except Exception:
                payload = {}
            try:
                _job_q.put_nowait(payload)
            except queue.Full:
                self.send_response(429)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(204)
            self.end_headers()
        elif self.path == "/cancel":
            if not self._check_token():
                return
            result = _cancel_current()
            body = json.dumps(result).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/shutdown":
            if not self._check_token():
                return
            self.send_response(204)
            self.end_headers()
            threading.Thread(target=self.server.shutdown, daemon=True).start()
        else:
            self.send_response(404)
            self.end_headers()


def _write_state(port, token):
    # daemon.state holds the auth token (a credential), so create it owner-only
    # (0o600) and atomically truncate. On POSIX this keeps the token unreadable
    # by other local users; on Windows the mode bits are a best-effort no-op but
    # the file already lives under the user profile.
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(
        {"pid": os.getpid(), "port": port, "started": time.time(), "token": token}
    )
    fd = os.open(str(STATE_FILE), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)


def _cleanup_state():
    try:
        STATE_FILE.unlink()
    except OSError:
        pass


def _hotkey_listener():
    """Background thread that registers a global hotkey for /cancel via the
    `keyboard` Python library. Silent no-op if the library isn't installed.
    Hotkey string is read from the speech.silence_hotkey config key
    (resolves against global+project settings; project takes precedence)
    once at startup — restart the daemon to pick up a changed hotkey.

    The `keyboard` library captures OS-level key events. On Windows it runs
    in user-mode without admin; some antivirus tools may flag it. If you
    need to swap libs (e.g. to `global_hotkeys`), this is the only place
    that touches it.
    """
    try:
        import keyboard
    except ImportError:
        return
    # Resolve hotkey from config (use the cwd-less path → only global + defaults).
    cfg = _load_config(None)
    hotkey = (cfg.get("silence_hotkey") or DEFAULTS["silence_hotkey"]).strip().lower()
    if not hotkey or hotkey == "none":
        return
    try:
        keyboard.add_hotkey(hotkey, _cancel_current)
        # Block forever — daemon won't exit because this thread is daemon=True
        # and won't keep the process alive.
        keyboard.wait()
    except Exception:
        # Hotkey conflict / permission denied / etc. — fail open.
        pass


def main():
    # Bind + write state file FIRST (under ~50 ms cold) so SessionStart's
    # readiness probe succeeds quickly. Pre-warming the edge_tts import
    # happens on a background thread so it doesn't gate listening.
    global _DAEMON_TOKEN
    _DAEMON_TOKEN = secrets.token_hex(16)
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    _write_state(port, _DAEMON_TOKEN)
    threading.Thread(target=_prewarm, daemon=True, name="tts-prewarm").start()
    threading.Thread(target=_hotkey_listener, daemon=True, name="tts-hotkey").start()
    try:
        server.serve_forever()
    finally:
        _cleanup_state()


if __name__ == "__main__":
    main()
