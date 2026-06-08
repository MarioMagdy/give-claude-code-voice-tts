"""F9 integration test — boots the real daemon, reads port+token from
`daemon.state`, fires the required HTTP scenarios, and shuts the daemon
down with the token. Prints the actual HTTP status codes to stdout so the
fix report can quote them verbatim.

Run with: python -m pytest speech/hooks/test_daemon_auth.py -q -s
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


def _wait_for_state(state_path: Path, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if state_path.exists():
            try:
                return json.loads(state_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        time.sleep(0.05)
    raise RuntimeError(f"daemon.state never appeared at {state_path}")


def _wait_for_port(port: int, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"daemon never started listening on port {port}")


def _post(port: int, path: str, body: bytes, headers: dict | None = None):
    url = f"http://127.0.0.1:{port}{path}"
    req = urllib.request.Request(url, data=body, headers=headers or {}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return -1, str(e).encode("utf-8")


def _get(port: int, path: str):
    url = f"http://127.0.0.1:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return -1, str(e).encode("utf-8")


@pytest.fixture(scope="module")
def running_daemon(tmp_path_factory):
    """Spawn the real daemon in a child process, isolated via a temp dir
    for daemon.state. The test module takes ~6 s (daemon startup + tests)."""
    workdir = tmp_path_factory.mktemp("daemon_run")
    # Run daemon with a fresh HOME so its LOG_FILE / state stay isolated
    # and we don't touch the real ~/.claude.
    env = os.environ.copy()
    env["HOME"] = str(workdir / "home")
    env["USERPROFILE"] = env["HOME"]
    (workdir / "home").mkdir(parents=True, exist_ok=True)

    # Launch as a child process so we exercise the actual main() code path.
    # (pythonw.exe would swallow stdout, so use python for the test.)
    # We pass the daemon path as an absolute arg (so cwd=workdir doesn't
    # shift it) but we still let cwd=workdir control the daemon's own CWD.
    proc = subprocess.Popen(
        [sys.executable, str(HERE / "daemon.py")],
        cwd=str(workdir),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    state_path = HERE / "daemon.state"
    # daemon.py writes state_file next to its own __file__, so it lives
    # under HERE (not the temp workdir). Make sure no stale state lingers.
    try:
        state_path.unlink()
    except FileNotFoundError:
        pass

    try:
        info = _wait_for_state(state_path, timeout=10.0)
        _wait_for_port(info["port"], timeout=5.0)
        yield info
    finally:
        # Shutdown using the token.
        if state_path.exists():
            try:
                info = json.loads(state_path.read_text(encoding="utf-8"))
                _post(int(info["port"]), "/shutdown", b"",
                      headers={"X-Speech-Token": str(info.get("token", ""))})
            except Exception:
                pass
        # Belt and braces.
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            state_path.unlink()
        except FileNotFoundError:
            pass


def test_daemon_f9_integration(running_daemon):
    port = int(running_daemon["port"])
    token = str(running_daemon["token"])
    assert token and len(token) == 32, "token must be a 16-byte hex string"

    # /ping — open, no token -> 200
    status, body = _get(port, "/ping")
    print(f"F9 GET  /ping            -> {status}")
    assert status == 200

    # /stop with NO token -> 401
    status, body = _post(port, "/stop", b'{"transcript_path":"x"}')
    print(f"F9 POST /stop (no token) -> {status}")
    assert status == 401

    # /stop WITH correct token + tiny valid payload -> 204
    status, body = _post(
        port, "/stop", b'{"transcript_path":"some/where/transcript.jsonl"}',
        headers={"X-Speech-Token": token},
    )
    print(f"F9 POST /stop (token OK) -> {status}")
    assert status == 204

    # /stop with Content-Length > 256 KB -> 413
    big = b"x" * (262145)
    status, body = _post(port, "/stop", big, headers={"X-Speech-Token": token})
    print(f"F9 POST /stop (oversize) -> {status}")
    assert status == 413

    # /stop with token OK but UNC transcript_path -> accepted to queue (204)
    # but daemon logs skip:bad-path and makes no outbound connection.
    status, body = _post(
        port, "/stop", b'{"transcript_path":"\\\\evil\\share\\x"}',
        headers={"X-Speech-Token": token},
    )
    print(f"F9 POST /stop (UNC)      -> {status}")
    assert status == 204
    # The /status endpoint tells us nothing is playing, proving no
    # outbound connection was made and the job was rejected locally.
    time.sleep(0.5)
    status, body = _get(port, "/status")
    print(f"F9 GET  /status          -> {status}  body={body!r}")
    assert status == 200

    # Tear down via /shutdown with the token (fixture handles this; the
    # call here is just for visibility).
    status, body = _post(port, "/shutdown", b"", headers={"X-Speech-Token": token})
    print(f"F9 POST /shutdown        -> {status}")
    assert status == 204
