# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

No unreleased changes yet.

## [1.0.0] - 2026-06-09

Initial public release.

### Added

- Claude Code plugin (`voice-tts@give-claude-code-voice-tts`) that gives
  Claude Code spoken output on Windows: a `Stop` hook plus a persistent
  `edge-tts` daemon read a `<spoken>…</spoken>` summary of each conversational
  reply aloud. Off by default everywhere; opt in per project.
- Persistent localhost daemon (`hooks/daemon.py`) that keeps a warm
  interpreter and a pre-imported `edge_tts`, serializing playback through a
  single queue so overlapping sessions never talk over each other.
- `speech` skill and `/speech` control menu (`hooks/voices.py`) to switch
  voices, change voice by "vibe" (calmer, more British, male, expressive),
  preview/sample the curated voice catalogue, mute or unmute, and interrupt
  audio that's currently playing (`Ctrl+Alt+S` or `silence`).
- `SessionStart` hooks that ensure the daemon is running, roll a fresh
  per-session voice, and (only where speech is enabled for the project)
  inject the `<spoken>` emission rule as additional context — no global
  `CLAUDE.md` edit required.
- `SessionEnd` hook that stops any in-flight audio on exit.

### Security

- The daemon's `daemon.state` file (which holds its localhost auth token) is
  created owner-only (`0o600`) via atomic truncate, so other local users on
  the same machine can't read the credential.
- Daemon endpoints (`/stop`, `/cancel`, `/shutdown`) require a matching
  `X-Speech-Token` header; request bodies are capped at 256 KB and the job
  queue is bounded so a flood of requests can't grow memory unbounded.
- The PowerShell `SessionStart` hooks (`session_start.ps1`,
  `speech_status.ps1`) validate the project `cwd` from the hook payload
  before touching the filesystem, rejecting UNC paths and the mixed-slash
  forms Windows also normalizes to UNC (`/\host\share`, `\/host/share`) —
  closing a path that could otherwise trigger an outbound SMB read and leak
  an NTLM hash. The equivalent Python-side guard covers `daemon.py` and
  `hook_worker.py`.

### Changed

- Renamed the repository and marketplace to `give-claude-code-voice-tts` and
  the plugin id to `voice-tts` (previously `claude-code-voice-tts`), so the
  install command is `/plugin install voice-tts@give-claude-code-voice-tts`.
  The old GitHub URL redirects automatically.
