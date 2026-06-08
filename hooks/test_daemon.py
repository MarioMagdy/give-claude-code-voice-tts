"""Tests for speech/hooks fixes F7 (daemon._log_job) and F8 (hook_worker
default-OFF + session-state precedence).

Run with: python -m pytest speech/hooks/ -q
"""
import importlib
import json
import sys
import tempfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SPEECH = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(SPEECH))

import daemon  # noqa: E402
import hook_worker  # noqa: E402


# ---------------------------------------------------------------------------
# F7 — _log_job: writes to a fixed per-user log, never under cwd, no text.
# ---------------------------------------------------------------------------

def test_log_job_ignores_cwd_and_text(tmp_path, monkeypatch):
    # Redirect Path.home() so we don't touch the real ~/.claude on this box.
    monkeypatch.setattr(daemon, "LOG_FILE", tmp_path / "speech-daemon.log")
    daemon._log_job("/some/cwd", "en-US-TestVoice", "played", "secret text")
    # (1) nothing was created under the cwd
    assert not (Path("/some/cwd") / ".claude").exists(), (
        "no file should be written under the cwd"
    )
    # (2) the per-user log file exists and was written
    log = tmp_path / "speech-daemon.log"
    assert log.exists(), "per-user log file should be created"
    line = log.read_text(encoding="utf-8")
    # (3) voice + status are present
    assert "en-US-TestVoice" in line
    assert "played" in line
    # (4) spoken text is NEVER in the log
    assert "secret text" not in line, (
        f"spoken text leaked into log: {line!r}"
    )
    assert "text=" not in line, f"text= field still present: {line!r}"


def test_log_job_signature_compat():
    """Caller compatibility — the signature still accepts (cwd, voice, status, text_snippet=)."""
    import inspect
    sig = inspect.signature(daemon._log_job)
    assert list(sig.parameters.keys())[:4] == ["cwd", "voice", "status", "text_snippet"]


# ---------------------------------------------------------------------------
# F8 — hook_worker: default OFF, session-state beats project settings.
# ---------------------------------------------------------------------------

def test_hook_worker_default_off():
    """With no settings anywhere, resolved enabled must be False (F8)."""
    with tempfile.TemporaryDirectory() as d:
        # Point at a clean empty home + project dir.
        tmp = Path(d)
        # Patch the module-level constants.
        import hook_worker as hw
        original_global = hw.GLOBAL_SETTINGS
        original_dep = dict(hw.DEFAULTS)
        try:
            hw.GLOBAL_SETTINGS = tmp / "user-settings.json"
            assert not hw.GLOBAL_SETTINGS.exists()
            cfg = hw._load_config(str(tmp / "some-project"))
            assert cfg.get("enabled") is False, (
                f"default must be OFF, got cfg={cfg!r}"
            )
        finally:
            hw.GLOBAL_SETTINGS = original_global
            hw.DEFAULTS.clear()
            hw.DEFAULTS.update(original_dep)


def test_hook_worker_session_state_beats_project():
    """project enabled:true but session-state enabled:false -> resolved = False
    (F8: session-state is highest precedence)."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        proj = tmp / "proj"
        proj_claude = proj / ".claude"
        proj_claude.mkdir(parents=True)
        # Project pins enabled=True
        (proj_claude / "settings.json").write_text(
            json.dumps({"speech": {"enabled": True}}), encoding="utf-8"
        )
        # Session-state overrides to False
        (proj_claude / "session-state.json").write_text(
            json.dumps({"enabled": False}), encoding="utf-8"
        )

        import hook_worker as hw
        original_global = hw.GLOBAL_SETTINGS
        try:
            hw.GLOBAL_SETTINGS = tmp / "user-settings.json"  # missing
            cfg = hw._load_config(str(proj))
            assert cfg.get("enabled") is False, (
                f"session-state must win over project, got cfg={cfg!r}"
            )
        finally:
            hw.GLOBAL_SETTINGS = original_global


def test_hook_worker_default_constant_is_false():
    """The DEFAULTS dict itself must have enabled:False (F8 hard requirement)."""
    assert hook_worker.DEFAULTS.get("enabled") is False, (
        f"DEFAULTS['enabled'] must be False, got {hook_worker.DEFAULTS!r}"
    )
