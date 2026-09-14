# Security

This document describes the real threat model of `give-claude-code-voice-tts`,
a Claude Code plugin that speaks a short `<spoken>` summary of each reply aloud
on Windows.

It ships a localhost HTTP daemon. That deserves scrutiny, so every claim below
names the file and line that enforces it. Where a protection is partial, this
document says so. Section 5 (data leaving the machine) is the disclosure that
matters most; read it even if you skip the rest.

---

## 1. Architecture and trust boundary

Four processes are involved:

| Process | Started by | Role |
|---|---|---|
| `hooks/daemon.py` | `hooks/session_start.ps1:56` (SessionStart, via `pythonw.exe`) | Long-lived HTTP server; synthesises and plays audio |
| `hooks/hook_post.py` | Stop hook (`hooks/hooks.json:22`) | Reads the Stop payload on stdin, POSTs it to the daemon |
| `hooks/session_end.ps1` | SessionEnd hook (`hooks/hooks.json:32`) | POSTs `/cancel` so audio stops when you exit |
| `hooks/voices.py` | The `speech` skill / you | Control surface: enable, disable, switch voice, silence |

All four run as the invoking desktop user. There is no service, no elevation,
no scheduled task, and nothing installed outside the plugin directory and
`~/.claude`.

**Transport and bind address.** The daemon binds `127.0.0.1` on an ephemeral
port (`hooks/daemon.py:563`):

```python
server = HTTPServer(("127.0.0.1", 0), _Handler)
```

Being precise about what that does and does not mean:

- It **does** exclude the LAN, the VPN, and every other machine. The socket is
  bound to the loopback interface only, not `0.0.0.0`, so no off-host peer can
  connect. It is not exposed by an IPv6 wildcard either — the literal
  `127.0.0.1` gives an IPv4 loopback socket.
- It **does not** exclude anything running on the same machine. Every local
  process, of any user, can connect to the port. Loopback binding is not an
  authorisation boundary; it is a reachability boundary. Authentication
  (section 2) is what separates callers.
- It **does not** exclude other sessions on a terminal server, RDP host, or any
  multi-user Windows install. On such a machine, "localhost" includes everyone
  logged in. See section 7.

**What actually travels over HTTP.** This is the part people usually guess
wrong. The reply text is *not* POSTed to the daemon. `hook_post.py` forwards
Claude Code's Stop-hook JSON verbatim (`hooks/hook_post.py:24,43`), which
carries `cwd` and `transcript_path` — file paths, not content. The daemon then
opens that transcript from disk itself (`hooks/daemon.py:360,164-192`), finds
the last non-sidechain assistant turn, and extracts the `<spoken>` block
(`hooks/daemon.py:364-368`). So the HTTP body is a small JSON control message;
the text to be spoken never crosses the socket.

**Endpoints** (`hooks/daemon.py:445-502`):

| Endpoint | Auth | Purpose |
|---|---|---|
| `POST /stop` | token required (`daemon.py:466`) | Queue a speech job |
| `POST /cancel` | token required (`daemon.py:485`) | Stop playback, drain queue |
| `POST /shutdown` | token required (`daemon.py:495`) | Stop the daemon |
| `GET /ping` | **none** (`daemon.py:446`) | Liveness probe |
| `GET /status` | **none** (`daemon.py:451`) | Returns `{"playing": bool}` |

The two GETs are deliberately open so the SessionStart readiness probe
(`hooks/session_start.ps1:45`) is cheap. They disclose that the daemon exists
and whether audio is playing right now. Nothing else. No action can be taken
through them.

---

## 2. Authentication

**Generation.** A fresh token per daemon process, from the OS CSPRNG
(`hooks/daemon.py:562`):

```python
_DAEMON_TOKEN = secrets.token_hex(16)
```

128 bits of entropy, hex-encoded to 32 characters. It is never derived from the
username, path, PID, or time. It lives only in daemon memory and in the state
file; it is not persisted across restarts, so killing and restarting the daemon
invalidates it.

**Storage.** Written to `hooks/daemon.state` next to the daemon, together with
the PID and port (`hooks/daemon.py:505-516`), created with
`os.open(..., O_WRONLY|O_CREAT|O_TRUNC, 0o600)`. The file is removed when the
daemon exits cleanly (`hooks/daemon.py:519-522,571`) and is gitignored
(`.gitignore:7`) so it cannot be committed.

Be clear about what `0o600` buys on the target platform: **on Windows those mode
bits are close to a no-op.** CPython maps the mode to the read-only attribute
only; the file inherits the containing directory's ACL. The meaningful
protection on Windows is that the plugin directory normally lives under the user
profile, which is not readable by other non-administrative users by default. If
you install the plugin into a shared location (`C:\ProgramData`, a shared drive,
a directory with loosened ACLs), that protection is gone and any local user can
read the token. On POSIX the `0o600` is real, though the playback path is
Windows-only anyway.

**Verification** is constant-time (`hooks/daemon.py:438`):

```python
ok = hmac.compare_digest(presented.encode("utf-8"), _DAEMON_TOKEN.encode("utf-8"))
```

A mismatch returns 401 with no body (`hooks/daemon.py:440-443`); the request
body is never read, because the token check runs before `_read_body`
(`hooks/daemon.py:466-468`).

`hooks/test_daemon_auth.py` boots the real daemon and asserts the token is a
32-character hex string (line 131), that `POST /stop` without a token returns
401 (lines 139-141), and that with the token it returns 204 (lines 144-149).

### What the token actually defends against

It is important not to oversell this.

**The token does not stop a hostile local process running as you.** Any code
executing under your user account can read `daemon.state` and obtain the token,
exactly as the hooks do. The token is not a sandbox. On a single-user desktop —
the target environment — anything that can read your files can drive the daemon.
If you are already running hostile code as yourself, this daemon is not your
problem; it can already read your source, your SSH keys, and your browser
profile.

**What the token does stop is the browser-driven case**, which is the realistic
remote threat against any localhost server:

- A web page cannot read `daemon.state`, so it cannot produce the token.
- `POST /stop` requires the custom header `X-Speech-Token`
  (`hooks/daemon.py:435`). A custom header on a cross-origin request forces a
  CORS preflight. The daemon implements no `do_OPTIONS`, so the preflight fails
  and the browser never sends the request.
- The request shapes that a browser *can* send cross-origin without preflight —
  a form POST, an image or script tag — cannot set that header, so they reach
  the handler and are rejected with 401.
- The daemon sends no `Access-Control-Allow-Origin` header anywhere, so even
  the open GETs are not readable cross-origin.

This also covers DNS rebinding, which is the standard bypass for "it's only
listening on localhost". Rebinding can make a page's origin resolve to
`127.0.0.1` and defeat the same-origin check, but it still gives the attacker no
way to learn a 128-bit secret stored in a local file. They would gain the
ability to read `/ping` and `/status` — that the daemon is up, and whether it is
currently speaking — and nothing more.

**Not implemented:** `Host` and `Origin` header validation. It would be a
reasonable belt-and-braces addition and is not there today. The token is what is
relied on.

---

## 3. Input handling

**Body size.** `POST` bodies are capped at 256 KB (`hooks/daemon.py:47`).
`_read_body` (`hooks/daemon.py:416-431`) reads `Content-Length`, treats a
malformed or negative value as `0`, returns 413 without reading anything if it
exceeds the cap, and otherwise reads exactly that many bytes. A request with no
`Content-Length` (for example chunked encoding) is treated as a zero-length
body, so there is no path to an unbounded read. `hooks/test_daemon_auth.py:152-155`
asserts the 413 on a 256 KB + 1 body.

**Malformed JSON** never raises: a parse failure yields an empty payload
(`hooks/daemon.py:471-474`), the job is queued, and `_process_job` discards it
at the first guard. A `Content-Type` other than JSON is not checked and does not
matter, since the body is parsed as JSON regardless.

**Queue pressure.** Jobs go into a bounded queue of 64
(`hooks/daemon.py:316`); a full queue returns 429 (`hooks/daemon.py:476-481`)
rather than growing memory. Exactly one job is processed at a time
(`hooks/daemon.py:392-402`), so concurrent Claude Code sessions cannot produce
overlapping audio.

**Path validation.** Both path fields in the payload (`transcript_path` and
`cwd`) are checked by `_is_local_path` (`hooks/daemon.py:136-149`) before any
I/O touches them. The guard rejects empty paths and **any** path beginning with
two path separators, in all four combinations — `\\host`, `//host`, `/\host`,
`\/host`:

```python
if len(s) >= 2 and s[0] in ("\\", "/") and s[1] in ("\\", "/"):
    return False
```

The mixed forms matter because Windows normalises all four to a UNC path. The
risk being closed is specific: reading `\\attacker\share\...` causes an outbound
SMB connection, and Windows will attempt NTLM authentication to that host,
leaking a hash usable for relay or offline cracking. `_process_job` applies the
guard to `transcript_path` before any `open()` and to `cwd` before any settings
read (`hooks/daemon.py:345-356`), then additionally requires the transcript to
exist and be a regular file (`hooks/daemon.py:351`). The same guard is mirrored
in `hooks/hook_worker.py:52-65`, `hooks/voices.py:44-54`,
`hooks/session_start.ps1:14-21`, and `hooks/speech_status.ps1:16-23`.

Tests: `hooks/test_daemon.py:153-176` covers the helper including the mixed-slash
forms and asserts that `os.path.normpath` really does turn them into UNC;
`test_daemon.py:185-217` monkeypatches `open` and asserts `_process_job` opens
nothing at all for a UNC transcript, logging `skip:bad-path`;
`test_daemon.py:122-150` asserts a UNC `cwd` yields byte-identical config to no
`cwd` at all, proving the remote settings file is never read.

**Known limit of this guard.** It is a prefix check on the string, not a
resolution of the final target. A mapped network drive letter (`Z:\` bound to
`\\host\share`), an NTFS junction, or a symlink pointing at remote storage
passes the check and would still produce remote I/O. Closing that properly means
resolving the path and querying the drive type, which is not implemented. If you
work from mapped network drives, be aware of this.

**Command injection.** There is none in the speech path, by construction. The
spoken text is passed to `edge_tts.Communicate(...)` as a Python argument
(`hooks/daemon.py:234-239`) and never touches a shell. The only string
interpolated into an MCI command (`hooks/daemon.py:265`) is the mp3 path, which
the daemon generates itself from the temp directory, its own PID, and a
timestamp (`hooks/daemon.py:377`) — never from the request.

The markdown stripper `_sanitize_for_speech` (`hooks/daemon.py:82-102`) is a
listening-quality feature, not a security control. It exists so TTS does not
read backticks and asterisks aloud. Do not count it as input sanitisation.

---

## 4. What is and is not logged

The daemon appends one line per job to `~/.claude/speech-daemon.log`
(`hooks/daemon.py:319`). `_log_job` (`hooks/daemon.py:322-335`) writes exactly
this and nothing else:

```python
f.write(f"{ts}  voice={voice}  status={status}\n")
```

Three fields: local timestamp, voice id, status string (`played`,
`skip:disabled`, `skip:no-tag`, `skip:bad-path`, `error:<ExceptionType>`, …).

**The spoken text is not logged.** The function still accepts a `text_snippet`
argument for call-site compatibility and deliberately ignores it — call sites at
`hooks/daemon.py:381,383` pass the spoken text and it is dropped on the floor.
`hooks/test_daemon.py:27-46` is the regression test: it calls
`_log_job("/some/cwd", "en-US-TestVoice", "played", "secret text")` and asserts
`"secret text" not in line` and `"text=" not in line`. The same test asserts
nothing is written under the supplied `cwd` — the log path is fixed to the user
profile and never follows project input.

Note what this means in the other direction: the log does record **when** you
were using Claude Code with speech enabled, at one line per spoken reply, and it
is not rotated or size-capped. It is created with default permissions inside
`~/.claude`. Delete it whenever you like; nothing depends on it.

The daemon also suppresses the stdlib per-request access log
(`hooks/daemon.py:412-414`), so no request lines, paths, or headers are written
anywhere.

---

## 5. Data leaving your machine

**Read this section before enabling speech.**

Synthesis is not local. `hooks/daemon.py:231-240` calls
`edge_tts.Communicate(text, voice, ...)`, and the `edge-tts` package
(a required dependency, `README.md:84`) sends the text over a TLS WebSocket to
Microsoft's Edge "read aloud" endpoint:

```
wss://speech.platform.bing.com/consumer/speech/synthesize/readaloud/edge/v1
```

(from `edge_tts/constants.py` in edge-tts 7.2.8), which returns the audio.

So, plainly: **the contents of every `<spoken>` block leave your computer and
reach Microsoft.** That text is a summary written by Claude about your work. It
can and will contain project names, file names, technical details of what you
are building, and whatever else the model chose to say aloud. This happens on
every spoken reply, automatically, with no further prompt.

What is *not* sent: your source code, your transcript, your prompts, your file
paths, and the non-spoken portion of Claude's replies. Only the extracted
`<spoken>` text is passed to synthesis (`hooks/daemon.py:364-373,379`). The
`<spoken>` convention itself is meant to be a one-or-two-sentence conversational
summary, and the plugin instructs the model to keep data-shaped content out of
it — but that is a model instruction, not an enforced boundary. Treat the
`<spoken>` text as content you are publishing to a third party.

There is no API key and no account, which means there is also no contract,
no data-processing agreement, and no documented retention policy covering this
traffic. It is the same free consumer endpoint that Microsoft Edge's read-aloud
feature uses.

**Do not enable speech on work where sending a description of that work to
Microsoft is unacceptable** — client code under NDA, classified or regulated
environments, security research on undisclosed issues, anything covered by a
policy that restricts third-party processing. This is not a hypothetical caveat;
it is the plugin's normal operating mode.

If you need speech without that egress, `CONTINGENCIES.md` sections C1 tier 3
and tier 4 describe swapping the synthesis backend for Windows SAPI (offline,
built in) or Piper (offline neural). Neither is wired up today — treat them as a
documented path, not a shipped option.

Apart from edge-tts, the plugin makes no other network calls. No telemetry, no
update check, no analytics. The only other sockets are loopback POSTs between
the hooks and the daemon.

---

## 6. Default posture and blast radius

Speech is **off by default and opted into per project.** This is enforced in
four independent places, all of which default to false:

- `hooks/daemon.py:52` — `DEFAULTS = {"enabled": False, ...}`
- `hooks/hook_worker.py:20` — same
- `hooks/voices.py:196` — `return False, "hardcoded default (off; opt-in per project)"`
- `hooks/speech_status.ps1:52` — `return $false  # OFF by default`

Resolution order is session-state > project `.claude/settings.json` > user
`~/.claude/settings.json` > the off default (`hooks/daemon.py:152-161`).
`hooks/test_daemon.py:60-79` asserts the resolved value is `False` with no
settings anywhere, and `test_daemon.py:111-115` asserts the constant itself.

Consequences of being off in a project:

- `_process_job` returns before reading the transcript, before synthesis, and
  before any network call (`hooks/daemon.py:357-359`). Nothing is spoken and
  nothing is sent.
- `speech_status.ps1` injects no context, so the model is not even told to emit
  `<spoken>` tags there (`hooks/speech_status.ps1:55-58`).
- `voices.py session-random` self-gates on the resolved flag and writes nothing,
  so opening an unrelated repository does not litter it with a
  `.claude/session-state.json` (`hooks/voices.py:461-467`).

The daemon itself *is* started on SessionStart regardless of whether speech is
enabled for that project (`hooks/session_start.ps1:52-66`), because it is a
singleton shared across sessions. A listening socket therefore exists once you
install the plugin, even in projects where speech is off.

An ephemeral `enable` deliberately does not survive into the next session: the
session-random roll drops any stale `enabled` override so it reverts to the
pinned project setting or to off (`hooks/voices.py:476-480`).

**Optional component with a real footprint.** If the `keyboard` package is
installed, the daemon starts a global hotkey listener for the interrupt shortcut
(`hooks/daemon.py:526-554`, default `ctrl+alt+s`). On Windows that library
installs a low-level system-wide keyboard hook, which means the daemon process
receives *every* keystroke on the desktop in order to match that one chord. The
plugin uses it only to call `_cancel_current`, and the import is wrapped so a
missing library is a silent no-op (`hooks/daemon.py:538-541`). Still: this is a
process with a global keyboard hook, some endpoint-protection products will flag
it as such, and it is a meaningfully larger footprint than the rest of the
plugin. It is optional — `pip install keyboard` is listed as optional in
`README.md:85`. Skip it if you would rather not have that, and use the skill's
`silence` command instead of the hotkey.

---

## 7. Non-goals

This plugin does not defend against, and makes no attempt to defend against:

- **A hostile process running as your user.** It can read `daemon.state`,
  obtain the token, and drive every endpoint. It could also simply read the
  files the daemon reads. The token is an origin control, not a sandbox.
- **A compromised machine.** Nothing here is a mitigation for that.
- **Multi-user and shared hosts, RDP, terminal servers, shared build agents.**
  The loopback socket is reachable by every logged-in session on the box, and
  the token's confidentiality depends entirely on the file ACL of the plugin
  directory. This plugin is designed for a single-user Windows desktop. Do not
  deploy it on a shared host.
- **A malicious Claude Code transcript.** `transcript_path` is only guarded for
  locality and file-ness (`hooks/daemon.py:345-351`); it is not constrained to
  the project directory. A caller that already has the token can point the
  daemon at any local file the user can read, and any `<spoken>` block found
  inside a line that parses as an assistant turn will be spoken and sent to
  Microsoft. That is a confused-deputy path from local file to network, but it
  requires local code execution as you, which already has both file access and
  network access. It is a limitation, not a privilege escalation.
- **Network-level confidentiality of the synthesis request.** It is TLS to
  Microsoft, with whatever trust store your Python uses. If your environment
  does TLS interception, the spoken text is visible to that middlebox too.
- **Supply chain.** `edge-tts`, its transitive dependencies (`aiohttp` and
  friends), and the optional `keyboard` package are installed from PyPI by you
  and are not pinned or verified by this plugin. Their security is their own.
- **Availability.** Every failure path is deliberately silent so TTS never
  blocks your next prompt (`hooks/hook_post.py:52-54`,
  `hooks/daemon.py:396-399`). A local process with the token can silence you,
  shut the daemon down, or fill the 64-slot queue. The consequence is no audio,
  which is the same as not installing the plugin.

---

## 8. Supported versions

| Version | Supported |
|---|---|
| 1.0.x | Yes |
| < 1.0 | No |

Fixes land on `main` and ship in the next release. There are no long-term
support branches. `plugin.json` carries the current version.

---

## 9. Reporting a vulnerability

Please report privately, **not** as a public GitHub issue.

Use GitHub private vulnerability reporting on this repository:
**Security tab → Report a vulnerability**
(<https://github.com/MarioMagdy/give-claude-code-voice-tts/security/advisories/new>).
That creates a private advisory visible only to the maintainer, and gives you a
thread to discuss a fix and coordinate disclosure.

Helpful things to include: the version or commit, what you did, what happened,
and — if it is not obvious — why it crosses a boundary this document claims to
hold. A minimal reproduction is worth more than a scanner report.

**Response expectations.** This is a hobby project maintained by one person in
their spare time. Realistically: an acknowledgement within about a week, and a
fix timeline agreed with you after that. There is no SLA and it would be
dishonest to publish one. If a week passes with no reply, a nudge on the
advisory thread is welcome.

Please give a reasonable window to ship a fix before disclosing publicly. If you
believe an issue is being actively exploited, say so in the report and it will
be prioritised.

### Out of scope

The following are documented behaviour, not vulnerabilities, and are covered
above:

- "A local process running as the user can read the token." Section 2.
- "The `<spoken>` text is sent to Microsoft." Section 5 — that is the design;
  reports that it is *undisclosed* or that *more than* the `<spoken>` text is
  sent are very much in scope.
- "`/ping` and `/status` are unauthenticated." Section 1.
- "It is insecure on a multi-user or terminal-server host." Section 7 — it is
  not supported there.

Findings that go beyond what this document claims are in scope, including: any
way to reach an authenticated endpoint without the token; any path that produces
remote I/O or an outbound SMB connection; any input that causes the daemon to
send text other than the `<spoken>` block to synthesis; anything that gets
spoken text or transcript content into the log file; and anything that lets a
web page in a browser drive the daemon.
