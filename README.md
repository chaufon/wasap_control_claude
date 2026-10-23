# Claude Code Remote Control via WhatsApp

Implementation of [`SPEC.md`](./SPEC.md). See that document for the full
design and rationale; this file only covers setup and running it.

## Prerequisites

```bash
sudo apt update
sudo apt install -y redis-server ffmpeg nodejs npm tmux
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
  the Pro subscription — the orchestrator runs `claude`, it never uses an API
  key.
- `tmux` (already listed above) hosts each project's shared live session
  (SPEC.md §7.7).
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
- `project_containers` — optional. Folders under `project_root`, as relative
  paths (e.g. `c`, `c/car`), that hold projects instead of being one. Each
  is scanned one level deep, and its projects are named by relative path
  (`c/whey`, `c/car/maintenance`). A missing container is skipped with a
  warning in the log. Restart the orchestrator after changing this list
  (SPEC.md §7.2).
- `remote_control` — optional. `permission_mode` (default
  `bypassPermissions`, so the session never asks for approval, whichever
  device drives it) and `chrome` (default `false`: no Claude in Chrome, so
  browser work uses Playwright MCP) for each project's shared live session
  (SPEC.md §7.7).
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
- **One session, three devices.** Each project has one live Claude Code
  session, shared by WhatsApp, the Claude app (Code tab, through Remote
  Control) and your computer (`tmux attach -t rc-<name>`). Whatever you send
  from one is part of the same conversation on the others. Only answers to
  messages you sent from WhatsApp come back to WhatsApp (SPEC.md §4.4, §7.7).
- `/project <name-or-number>` — switch active project, by name or by its
  number from `/projects`. Any project folder under `project_root` or one of
  its containers works immediately, with no separate registration step.
  `/project <container>` lists that container's projects (SPEC.md §7.2).
  It also starts the project's live session, and replies "📱 rc-c-whey is
  live in the Claude app" with its claude.ai link, plus the
  `tmux attach -t rc-c-whey` command that opens the same session in a
  terminal on your computer. To leave it, close the terminal window or tab:
  the session keeps running. `/exit` ends the session. A second message previews
  Claude's last answer in that project (200 characters). The session runs
  through your login shell, so it gets the same environment and settings as
  `claude` in a terminal, and resumes its last conversation when it starts
  again. Claude in Chrome is off, so browser work goes through the
  Playwright MCP plugin.
- `/projects` — list every available project (config.yaml plus every
  `project_root` project folder), numbered. Each container is an unnumbered
  `📁` header with its projects indented under it. Shows just the name for anything under
  `project_root` — a `config.yaml` project whose path falls outside it still
  shows its full path.
- `/create <name>` or `/create <container>/<name>` — create a new project
  folder under `project_root` or inside one of its containers and switch to
  it (only if `project_root` is configured; the folder name allows letters,
  numbers, `-` and `_` — SPEC.md §7.2). Like `/project`, it starts the new
  project's live session.
- `/close [name-or-number]` — stop a project's live session (the active
  project's when no argument is given). Sessions keep running when you
  switch to another project, survive orchestrator restarts, and end on
  reboot or logout; the next `/project` or WhatsApp message starts one
  again (SPEC.md §7.7).
- `/last` — Claude's full last answer in the active project, whichever
  device the question came from.
- `/stop` — drop your queued messages and interrupt the running turn (like
  Esc at the keyboard). A message sent while the session is busy waits in a
  queue and goes in once it's free ("⏳ Busy, queued").
- `/reconnect [name-or-number]` — connect a session to the Claude app again,
  e.g. after you archived it there. Archiving only disconnects the app; the
  session keeps working for WhatsApp and the computer.
- `/enable` / `/disable` — allow/stop messages from reaching Claude Code.
- `/status` — enabled/disabled state, voice replies, active project
  (busy/idle/queued), its session ID, the `tmux attach` command to open it on
  the computer, and every Remote Control session: 🟢 connected, 🟡 not
  connected (with the `/reconnect` to send), 🔴 exited, ⚪ started outside
  WhatsApp.
- `/c <text>` — type `<text>` into the session as is, e.g. `/c /usage`,
  `/c /grill-me an idea` or `/c /clear` — use this to reach Claude Code's own
  commands/skills, which would otherwise be swallowed as an unrecognized
  command above. Built-in commands like `/usage` reply with what they show on
  screen; `/c /clear` starts a fresh conversation and replies "🧹 Session
  cleared". While disabled, `/c` (and any other unrecognized `/command`)
  replies "Claude Code is disabled" instead of running — a leading `/` is
  never a personal note, so unlike plain text it's never silently ignored
  (SPEC.md §7.1/§7.3).
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
  about this convention when its session starts, so you can simply
  ask it to "send me the report" and it'll know where to put it. Anything
  placed there is delivered right after the reply to a WhatsApp message, then archived into
  `whatsapp-outbox/sent/` so it's never resent. Files over 64MB get a clear
  error instead of being sent. The first time it's used, `whatsapp-outbox/`
  gets its own `.gitignore` containing `*`, so it stays out of git
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
src/claude_orchestrator/   Python orchestrator (dispatcher, shared live sessions, hook relay, transcription, TTS, outgoing files, formatting, enable/disable state)
whatsapp-bridge/           Node.js/TypeScript Baileys <-> Redis bridge
config/config.example.yaml config template (copy to ~/.config/claude-orchestrator/config.yaml)
systemd/                   systemd --user unit files
```

## Changelog

Newest first. Details for each change are in SPEC.md and the git history.

### 2026-10-23

- WhatsApp, the Claude app and the computer share one live session per
  project: WhatsApp messages are typed into the project's
  `claude --remote-control` session in tmux, and answers come back through
  Claude Code hooks. Only answers to WhatsApp messages are sent to WhatsApp.
- Messages sent while the session is busy are queued; `/stop` drops the
  queue and interrupts the running turn.
- New `/last` and `/reconnect` commands. `/project` previews the last answer.
- `/new` is gone: `/c /clear` starts a fresh conversation. `/c /usage` and
  other built-in commands reply with what they show on screen.
- `/status` shows the `tmux attach` command and every Remote Control session
  with its state, including sessions started outside WhatsApp.
- Replies use bullets; `/help` is grouped into sections.
- `remote_control.spawn` is gone; new `redis.hooks_channel` (default
  `claude:hooks`).

### 2026-10-22

- `/project` and `/create` start a Claude Code Remote Control server for the
  project in a detached tmux session (`rc-<name>`), so the project opens in
  the Code tab of the Claude mobile app. The server runs through your login
  shell, in `bypassPermissions` mode, with Claude in Chrome off. Servers
  survive orchestrator restarts.
- New `/close [name-or-number]` command stops a project's server.
- `/status` lists the running servers.
- New `remote_control` config block: `permission_mode`, `spawn`, `chrome`.
- `tmux` is a prerequisite.
- Nested project containers: `project_containers` lists folders under
  `project_root` that hold projects (`c/whey`, `c/car/maintenance`).
  `/projects` groups projects under their container, and `/create` accepts
  `<container>/<name>`.

### 2026-08-18

- Voice replies speak a short spoken summary of the answer instead of the
  full text.
- GPU transcription uses the pip-installed CUDA libraries (`LD_LIBRARY_PATH`
  in the systemd unit).

### 2026-08-06

- Each project's Claude Code session ID persists across restarts.
- `/new` creates the replacement session right away.

### 2026-08-03

- The active project persists across restarts.
- `claude_code.binary` must be an absolute path.
- Restart and reload instructions for the services.

### 2026-07-28

- `/projects` shows just the name for projects under `project_root`.
- The bridge reconnects with exponential backoff and fetches the current
  WhatsApp protocol version at startup.
- Baileys upgraded to `^7.0.0-rc14`.

### 2026-07-27

- `/create` makes a new project folder and switches to it.
- `/c <text>` sends text to Claude Code verbatim.
- Voice replies (`/voice on`), outgoing files (`whatsapp-outbox/`) and
  reply/quote context.
- Every folder under `project_root` is a project, discovered live.

### 2026-07-26

- First version: WhatsApp self-chat messages (text, voice notes, files) go to
  Claude Code, and replies come back to WhatsApp.
