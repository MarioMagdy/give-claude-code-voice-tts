"""Unit tests for voices.py: enable/disable precedence, --global scope,
current --json, session-random gating, and cwd/--project targeting.

Runs against temp files (never touches the live .claude config). Run:
    python hooks/test_voices.py
"""
import contextlib
import io
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import voices  # noqa: E402

# voices.py is always invoked through main(), which forces UTF-8 stdout so the
# "→" in its messages prints on Windows cp1252 consoles. We call functions
# directly here, so reproduce that runtime condition.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _point_at(tmp: Path):
    voices.SESSION_STATE = tmp / "session-state.json"
    voices.PROJECT_SETTINGS = tmp / "settings.json"
    voices.USER_SETTINGS = tmp / "user-settings.json"


def _read(p):
    p = Path(p)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def test_enable_disable_precedence():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        _point_at(tmp)

        assert voices._resolve_enabled()[0] is False, "default should be OFF (opt-in)"

        voices.cmd_disable(SimpleNamespace(pin=False))
        assert _read(voices.SESSION_STATE).get("enabled") is False
        en, prov = voices._resolve_enabled()
        assert en is False and "session" in prov.lower(), (en, prov)

        voices.cmd_enable(SimpleNamespace(pin=False))
        assert _read(voices.SESSION_STATE).get("enabled") is True

        voices.cmd_disable(SimpleNamespace(pin=True))
        assert _read(voices.PROJECT_SETTINGS).get("speech", {}).get("enabled") is False
        assert "enabled" not in _read(voices.SESSION_STATE), "pin must clear session override"
        en, prov = voices._resolve_enabled()
        assert en is False and "project" in prov.lower(), (en, prov)

        voices.cmd_enable(SimpleNamespace(pin=False))
        en, prov = voices._resolve_enabled()
        assert en is True and "session" in prov.lower(), (en, prov)
    print("PASS: enable/disable precedence")


def test_global_scope():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        _point_at(tmp)
        # --global writes the USER settings layer.
        voices.cmd_disable(SimpleNamespace(pin=False, glob=True))
        assert _read(voices.USER_SETTINGS).get("speech", {}).get("enabled") is False
        en, prov = voices._resolve_enabled()
        assert en is False and "user" in prov.lower(), (en, prov)
        voices.cmd_enable(SimpleNamespace(pin=False, glob=True))
        assert _read(voices.USER_SETTINGS).get("speech", {}).get("enabled") is True
    print("PASS: --global scope")


def test_current_json():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        _point_at(tmp)
        voices.cmd_disable(SimpleNamespace(pin=True))  # project off
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            voices.cmd_current(SimpleNamespace(json=True))
        data = json.loads(buf.getvalue())
        assert data["enabled"] is False and isinstance(data["voice"], str), data
    print("PASS: current --json")


def test_session_random_gated():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        _point_at(tmp)
        # Disabled for the project -> roll must be a no-op (no session-state).
        voices.cmd_disable(SimpleNamespace(pin=True))
        assert not voices.SESSION_STATE.exists()
        with contextlib.redirect_stdout(io.StringIO()):
            voices.cmd_session_random(SimpleNamespace())
        assert not voices.SESSION_STATE.exists(), "off project should not get a voice roll"
        # Enabled -> roll writes a voice.
        voices.cmd_enable(SimpleNamespace(pin=True))
        with contextlib.redirect_stdout(io.StringIO()):
            voices.cmd_session_random(SimpleNamespace())
        assert isinstance(_read(voices.SESSION_STATE).get("voice"), str)
    print("PASS: session-random gated by enabled")


def test_project_targeting_subprocess():
    # Fresh process exercises the real cwd / --project resolution in main().
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        # --project override
        subprocess.run([sys.executable, str(HERE / "voices.py"),
                        "--project", str(tmp), "disable"],
                       capture_output=True, text=True, check=True)
        ss = tmp / ".claude" / "session-state.json"
        assert ss.exists() and _read(ss).get("enabled") is False, "--project should target tmp"
    with tempfile.TemporaryDirectory() as d2:
        tmp2 = Path(d2)
        # default = cwd
        subprocess.run([sys.executable, str(HERE / "voices.py"), "disable"],
                       capture_output=True, text=True, check=True, cwd=str(tmp2))
        ss2 = tmp2 / ".claude" / "session-state.json"
        assert ss2.exists() and _read(ss2).get("enabled") is False, "cwd should be the default target"
    print("PASS: cwd / --project targeting")


if __name__ == "__main__":
    test_enable_disable_precedence()
    test_global_scope()
    test_current_json()
    test_session_random_gated()
    test_project_targeting_subprocess()
    print("\nALL TESTS PASSED")
