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


# ---------------------------------------------------------------------------
# SC1 — _load_config must NOT follow UNC / non-local cwd (no outbound SMB read).
# ---------------------------------------------------------------------------

def test_load_config_rejects_unc_cwd_daemon(tmp_path, monkeypatch):
    """daemon._load_config(UNC) must return the same dict as _load_config(None)
    — i.e. the UNC cwd is NOT read."""
    base = daemon._load_config(None)
    unc = r"\\evil\share"
    unc2 = "//evil/share"
    assert base == daemon._load_config(unc), (
        f"UNC cwd should be ignored, got {daemon._load_config(unc)!r} vs {base!r}"
    )
    assert base == daemon._load_config(unc2), (
        f"//-prefixed cwd should be ignored, got {daemon._load_config(unc2)!r} vs {base!r}"
    )
    # Belt and braces: empty cwd also still works.
    assert base == daemon._load_config(""), f"empty cwd should be ignored: {daemon._load_config('')!r}"


def test_load_config_rejects_unc_cwd_hook_worker(tmp_path, monkeypatch):
    """hook_worker._load_config(UNC) must return the same dict as _load_config(None)."""
    import hook_worker as hw
    base = hw._load_config(None)
    unc = r"\\evil\share"
    unc2 = "//evil/share"
    assert base == hw._load_config(unc), (
        f"UNC cwd should be ignored in hook_worker, got {hw._load_config(unc)!r} vs {base!r}"
    )
    assert base == hw._load_config(unc2), (
        f"//-prefixed cwd should be ignored in hook_worker, got {hw._load_config(unc2)!r} vs {base!r}"
    )
    assert base == hw._load_config(""), f"empty cwd should be ignored: {hw._load_config('')!r}"


def test_is_local_path_helper():
    """The helper must correctly identify UNC and //-prefixed paths as not-local."""
    assert daemon._is_local_path(r"C:\Users\me") is True
    assert daemon._is_local_path("/home/me") is True
    assert daemon._is_local_path(r"\\evil\share") is False
    assert daemon._is_local_path("//evil/share") is False
    assert daemon._is_local_path("") is False
    assert daemon._is_local_path(None) is False


def test_is_local_path_rejects_mixed_slash_unc():
    """Council r2 (Gemini): Windows normalizes the mixed-slash forms /\\ and \\/
    to a UNC path too, so the guard must reject them — not just the literal
    \\\\ and // prefixes. `os.path.normpath('/\\evil\\share') == '\\\\evil\\share'`."""
    import os
    for p in (chr(47) + chr(92) + "evil" + chr(92) + "share",   # /\evil\share
              chr(92) + chr(47) + "evil" + chr(47) + "share"):   # \/evil/share
        assert daemon._is_local_path(p) is False, f"mixed-slash UNC must be rejected: {p!r}"
        # sanity: confirm Windows really would have resolved it to a UNC path
        norm = os.path.normpath(p)
        assert norm.startswith("\\\\") or norm.startswith("//"), norm
    # hook_worker mirrors the same guard
    import hook_worker as hw
    assert hw._is_local_path(chr(47) + chr(92) + "evil" + chr(92) + "share") is False


# ---------------------------------------------------------------------------
# SC2 — _process_job must reject a UNC transcript_path locally (skip:bad-path),
# NEVER read the file. The earlier integration test sent INVALID JSON and
# therefore never reached the guard. This unit test goes straight at the path.
# ---------------------------------------------------------------------------

def test_process_job_rejects_unc_transcript(monkeypatch, tmp_path):
    """A UNC transcript_path must short-circuit _process_job with skip:bad-path
    and never call open() / read the file. We also assert that the cwd-scoped
    config is never read (the SC1 fix is what makes that safe)."""
    read_calls = []
    real_open = daemon.open if hasattr(daemon, "open") else open  # noqa: A001

    def fake_open(path, *args, **kwargs):
        # If anything in _process_job tries to read the UNC path, fail loudly.
        s = str(path)
        if s.startswith("\\\\") or s.startswith("//"):
            raise AssertionError(f"UNC path was opened: {path!r}")
        read_calls.append(s)
        return real_open(path, *args, **kwargs)

    # Redirect the log to a temp file so we can assert the status line.
    monkeypatch.setattr(daemon, "LOG_FILE", tmp_path / "speech-daemon.log")

    # Re-bind builtins.open for the daemon module's namespace (the path guard
    # uses os.path.exists / os.path.isfile, but the actual file read inside
    # _last_assistant_text uses the builtin `open`).
    monkeypatch.setattr(daemon, "open", fake_open, raising=False)  # noqa: A001
    import builtins
    monkeypatch.setattr(builtins, "open", fake_open)

    payload = {"transcript_path": r"\\evil\share\transcript.jsonl", "cwd": r"\\evil\share"}
    # Should return without raising and without reading anything.
    daemon._process_job(payload)
    assert not read_calls, f"no file should have been opened, got {read_calls!r}"

    log = (tmp_path / "speech-daemon.log").read_text(encoding="utf-8")
    assert "skip:bad-path" in log, f"expected skip:bad-path in log, got: {log!r}"


def test_process_job_rejects_slash_prefixed_transcript(monkeypatch, tmp_path):
    """Same guard covers `//evil/share` style paths (non-Windows UNC)."""
    monkeypatch.setattr(daemon, "LOG_FILE", tmp_path / "speech-daemon.log")
    payload = {"transcript_path": "//evil/share/transcript.jsonl"}
    daemon._process_job(payload)
    log = (tmp_path / "speech-daemon.log").read_text(encoding="utf-8")
    assert "skip:bad-path" in log, f"expected skip:bad-path in log, got: {log!r}"
