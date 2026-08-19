# Claude Code Remote Control via WhatsApp

Implementation of [`SPEC.md`](./SPEC.md). See that document for the full
design and rationale; this file only covers setup and running it.

## Prerequisites

```bash
sudo apt update
sudo apt install -y redis-server ffmpeg nodejs npm
```

- **Node.js ≥ 20** (already present on this machine).
- **Python ≥ 3.11** with [`uv`](https://docs.astral.sh/uv/).
- **GPU-accelerated `faster-whisper`** needs CUDA/cuDNN libraries loadable at
  runtime. `faster-whisper`/`ctranslate2`, plus the `nvidia-cublas-cu12`/
  `nvidia-cudnn-cu12` pip wheels that provide those libraries, are all pulled
  in by `uv sync` below — no system-wide CUDA install is required. The
  systemd unit (`systemd/claude-orchestrator.service`) sets `LD_LIBRARY_PATH`
  to the venv's `nvidia/cublas/lib` and `nvidia/cudnn/lib` directories so
  those wheel-provided libraries are found at process start; if they aren't
  importable at runtime the transcriber automatically falls back to CPU
  (SPEC.md §4.3).
- `ffmpeg` (already listed above) is also used to convert synthesized voice
  replies to the Opus/OGG format WhatsApp voice notes require (SPEC.md §7.4).
  `piper-tts` and `langdetect` are pulled in by `uv sync` below; voice models
  themselves download automatically on first `/voice on` use.
- A logged-in Claude Code CLI session on this machine (`claude login`) under
  the Pro subscription — the orchestrator shells out to `claude`, it never
  uses an API key.
- **Disable automatic suspend/sleep** in system power settings (screen
  lock/screensaver is fine — see SPEC.md §2).

## Install

```bash
# Python orchestrator
uv sync

# Node WhatsApp bridge
cd whatsapp-bridge
npm install
npm run build
cd ..
```

## Configure

```bash
mkdir -p ~/.config/claude-orchestrator
cp config/config.example.yaml ~/.config/claude-orchestrator/config.yaml
```

Edit `~/.config/claude-orchestrator/config.yaml`:

- `whatsapp.allowed_sender_jid` — leave it as-is for the first run. WhatsApp
  gives your account two addresses — a phone-number JID
  (`...@s.whatsapp.net`) and a LID JID (`...@lid`) — and which one a given
  self-chat message arrives under depends on which client sent it (the
  phone app addresses it via LID, WhatsApp Desktop/Web via the phone-number
  JID; the bridge accepts messages addressed either way, see SPEC.md §4.1).
  `allowed_sender_jid` itself only needs the **phone-number JID** (`pn`) —
  that's also the address the bridge sends replies to. The bridge logs both
  as `{ pn, lid }` once you send yourself a WhatsApp message after pairing
  (see below); copy the `pn` value in and restart.
- `projects` — list of `{name, path}` pairs Claude Code is allowed to run
  against (SPEC.md §3, §4.4). Add every project you want reachable.
- `project_root` — optional. Every immediate, non-hidden folder under here
  becomes an available project automatically — no registration needed, just
  `/project <name>` — and it's also the only place `/create <name>` is
  allowed to make new project folders (SPEC.md §7.2). Leave unset to only
  use the static `projects` list above and disable `/create`.
- `tts.voices` — maps a detected reply language to a Piper voice id
  (defaults: `en_US-ljspeech-high`, `es_AR-daniela-high`). Only used once you
  send `/voice on` (SPEC.md §7.4); any other detected language falls back to
  `en`.
- No config needed for Claude Code sending files back — see "Using it"
  below and SPEC.md §7.5.

## Pair with WhatsApp

Run the bridge once interactively to scan the QR code:

```bash
cd whatsapp-bridge
npm run dev
```

Open WhatsApp on your phone → **Linked Devices** → **Link a device**, and
scan the QR code printed in the terminal. Once connected, send yourself a
message in your own "Message Yourself" chat and confirm the bridge logs
`{ pn, lid }` — copy the `pn` value into `allowed_sender_jid` in the
config, then stop this process (Ctrl-C).

Auth state persists under `bridge.auth_dir` (default
`~/.local/share/whatsapp-bridge/auth`), so re-pairing isn't needed on
restart.

## Run as services

Node installed via `nvm` (as opposed to a system package) doesn't live at
`/usr/bin/node`, and `systemctl --user` units don't source your shell's
`nvm.sh`/`.bashrc` — so `ExecStart=` in `systemd/whatsapp-bridge.service`
must be node's literal absolute path. Confirm it matches this machine
(`command -v node`, or `nvm which default`) before enabling the service,
and update the `ExecStart=` line if it doesn't.

```bash
mkdir -p ~/.config/systemd/user
cp systemd/*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now whatsapp-bridge.service claude-orchestrator.service
```

These are `systemctl --user` units, so they start on login and stop on
logout by design (SPEC.md §9/§10) — do **not** run
`loginctl enable-linger`, since the intent is to tie service lifetime to an
active login session.

### Reloading after changes

Neither service picks up code or config edits on its own — restart both
after pulling new code, editing `config.yaml`, or rebuilding the bridge
(`npm run build`):

```bash
systemctl --user restart whatsapp-bridge.service claude-orchestrator.service
```

If you only changed the unit files themselves (not their `ExecStart`
targets), run `systemctl --user daemon-reload` first.

If the bridge can't reconnect to WhatsApp after 10 attempts (~25 minutes of
exponential backoff, capped at 5 minutes per attempt — SPEC.md §4.1), it
exits cleanly and stays stopped rather than continuing to retry:

```bash
systemctl --user status whatsapp-bridge.service   # inactive (dead)
systemctl --user start whatsapp-bridge.service    # once the issue clears
```

Logs:

```bash
journalctl --user -u whatsapp-bridge.service -f
journalctl --user -u claude-orchestrator.service -f
tail -f ~/.local/share/claude-orchestrator/logs/orchestrator.log
```

## Using it

In your "Message Yourself" WhatsApp chat:

- While **enabled** (the default), any text, voice note, or file attachment
  goes to Claude Code running against the active project.
- Since this is your everyday personal number, that same chat is also
  where you'd jot personal notes — `/disable` stops anything from reaching
  Claude Code until you `/enable` again, so you can use the chat for both
  without a stray note accidentally becoming a command (SPEC.md §7.1).
  Disabled messages are simply left alone; your own WhatsApp history is
  the backup, nothing is copied elsewhere.
- `/project <name-or-number>` — switch active project, by name or by its
  number from `/projects`. Any folder under `project_root` works
  immediately, with no separate registration step (SPEC.md §7.2).
- `/projects` — list every available project (config.yaml plus every
  `project_root` folder), numbered. Shows just the name for anything under
  `project_root` — a `config.yaml` project whose path falls outside it still
  shows its full path.
- `/create <name>` — create a new project folder under `project_root` and
  switch to it (only if `project_root` is configured; flat names only, no
  slashes — SPEC.md §7.2).
- `/new` — reset the active project's Claude Code session.
- `/stop` — cancel the task currently running for the active project.
- `/enable` / `/disable` — allow/stop messages from reaching Claude Code.
- `/status` — enabled/disabled state, active project, busy/idle, and the
  active project's Claude Code session ID. Handy for continuing the same
  conversation from a terminal later: `claude --resume <session ID>` from
  that project's directory (a `/name`'d title only resolves in `--print`
  mode, not the interactive picker, so the ID is the reliable way to resume
  interactively).
- `/c <text>` — send `<text>` to Claude Code verbatim as the prompt, e.g.
  `/c /usage` or `/c /mattpocock-skills:grill-me an idea` — use this to reach
  Claude Code's own commands/skills, which would otherwise be swallowed as an
  unrecognized command above. Plugin skills need the fully-qualified
  `plugin:skill` name (`/mattpocock-skills:grill-me`, not just `/grill-me`) —
  Claude Code CLI itself replies "Unknown command" otherwise, same as it
  would interactively; this isn't something `/c` can work around. While
  disabled, `/c` (and any other unrecognized `/command`) replies "Claude Code
  is disabled" instead of running — a leading `/` is never a personal note,
  so unlike plain text it's never silently ignored (SPEC.md §7.1/§7.3).
- `/voice on` / `/voice off` — also send each reply as a WhatsApp voice note,
  in addition to the text reply (off by default). The voice note speaks a
  short (~100 word) spoken-style summary of the answer rather than reading
  the full text reply aloud; the text reply always carries the complete
  answer regardless. The voice note's language (English/Spanish, per
  `tts.voices` above) is detected from the reply text each time, not fixed
  to one language or based on how you asked (SPEC.md §7.4).
- `/help` — show the command list.
- Claude Code can also send **you** a file (e.g. a generated PDF report):
  it just needs to save it into `<project>/whatsapp-outbox/` — it's told
  about this convention automatically on every invocation, so you can simply
  ask it to "send me the report" and it'll know where to put it. Anything
  placed there is delivered right after the reply, then archived into
  `whatsapp-outbox/sent/` so it's never resent. Files over 64MB get a clear
  error instead of being sent. If the project is a git repo, `whatsapp-outbox/`
  is added to its `.gitignore` automatically the first time it's used
  (SPEC.md §7.5).
- WhatsApp's own **reply** (quote) feature works too: replying to an earlier
  message — text, an attachment, or a voice note, in either direction — adds
  that quoted content as context ahead of your new message, e.g. replying
  "make this black and white" to a photo re-downloads that photo into
  `whatsapp-inbox/` and tells Claude Code about it, and replying to text
  quotes it verbatim. Files saved into `whatsapp-inbox/`/`whatsapp-outbox/sent/`
  get a short content-hash suffix (e.g. `report_a3f9c2d1.pdf`) so two
  different files with the same name never collide (SPEC.md §5.9/§7.6).

## Repository layout

```
SPEC.md                    full technical specification
src/claude_orchestrator/   Python orchestrator (dispatcher, Claude Code CLI wrapper, transcription, TTS, outgoing files, formatting, enable/disable state)
whatsapp-bridge/           Node.js/TypeScript Baileys <-> Redis bridge
config/config.example.yaml config template (copy to ~/.config/claude-orchestrator/config.yaml)
systemd/                   systemd --user unit files
```
