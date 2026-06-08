"""Stop-hook entrypoint — Python flavour. Read stdin, look up the daemon's
port from daemon.state next to this script, POST the payload to /stop, exit.

Faster than the PowerShell variant (~250 ms vs ~1.3 s end-to-end) because
Python cold-start is ~5x faster than PowerShell cold-start. Only uses
stdlib (json + urllib) — no edge_tts, no asyncio, no third-party imports.

If anything goes wrong, exit silently with code 0 — TTS is non-critical
and we must never block the user's next prompt with an error.
"""
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "daemon.state"


def main():
    # 1. Read payload from stdin.
    try:
        payload = sys.stdin.read()
    except Exception:
        return
    if not payload:
        return

    # 2. Look up the daemon's port and auth token.
    try:
        info = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        port = int(info.get("port", 0))
        token = str(info.get("token", ""))
    except Exception:
        return
    if port <= 0 or not token:
        return

    # 3. POST to the daemon. 2 s ceiling — well under the hook's 5 s timeout.
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/stop",
        data=payload.encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Speech-Token": token,
        },
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=2).read()
    except (urllib.error.URLError, TimeoutError, OSError):
        # Daemon down or unreachable — silent skip.
        pass


if __name__ == "__main__":
    main()
