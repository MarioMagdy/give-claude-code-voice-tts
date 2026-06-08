# Spoken output (TTS)

A Stop hook on this machine reads `<spoken>…</spoken>` from the end of your
replies and speaks the contents aloud via a neural TTS. Use it so the session
feels conversational without forcing the user to listen to data dumps.

**Emit `<spoken>…</spoken>` at the very end of the reply when your message is
conversational** — an explanation, a recommendation, an opinion, a status
update, a question, a confirmation. Put inside it what you would actually say
if speaking. Length follows the content: a one-line confirmation is one
sentence; a substantive explanation can be a few sentences. Don't pad and
don't artificially truncate — the user said quality matters more than a word
count.

**Omit the tag entirely when the reply is primarily data to be shown, not
discussed** — a table, a code block, a file dump, an exact script, raw
command output, a long list. Reading those aloud is useless and annoying. If
there's a short conversational wrapper around the data ("here's the table;
the top row is X"), put just that wrapper in `<spoken>` — never the data
itself.

**Pure tool-call turns get nothing** — the hook already skips them.

**The tag is plumbing, not content.** Never narrate it, never put it
anywhere but the very end.

**No markdown syntax inside `<spoken>`. Words and punctuation only.** TTS
reads characters literally — backticks become "backtick", asterisks
become "asterisk", underscores become "underscore", and they ruin the
listening experience. Specifically, do NOT include inside the spoken tag:

- backticks (`` ` ``) around code identifiers — just say the word ("the
  spoken tag" not `` `<spoken>` tag``)
- asterisks for bold or italics (`**bold**`, `*emphasis*`)
- underscores for emphasis (`_x_`) — fine inside `snake_case` words
- markdown headers (`#`, `##`, `###`)
- bullet markers (`-`, `*`, `+` at line start)
- blockquote (`>`)
- code fences (` ``` `)
- link syntax (`[text](url)`)
- HTML-style tags like `<spoken>` itself or `<br>`

A worker-side sanitiser strips most of this as a safety net, but the
clean fix is just to write the spoken content in plain prose. If you
catch yourself reaching for markdown in there, stop — say it as if
you're speaking out loud, not writing.

## Voice & on/off control → use the speech skill

For anything about controlling spoken output — "stop speaking" / "mute" /
turning speech off or back on, switching the voice, changing the vibe
("calmer", "more British", "make it male"), previewing or sampling voices, or
interrupting audio that's playing right now — **use the `speech` skill**
(`.claude/skills/speech/SKILL.md`). It owns the full control surface and the
exact `hooks/voices.py` commands. Each new session auto-rolls a random
English-friendly voice; the skill explains how to override it, and the daemon
re-reads config on every job so changes take effect on the next reply.
