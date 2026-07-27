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
- **CUDA + cuDNN** matching the installed NVIDIA driver, for GPU-accelerated
  `faster-whisper`. `faster-whisper`/`ctranslate2` are pulled in by `uv sync`
  below; if CUDA libraries aren't importable at runtime the transcriber
  automatically falls back to CPU (SPEC.md §4.3).
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
- `project_root` — optional. Parent directory `/create <name>` is allowed
  to make new project folders under (SPEC.md §7.2). Leave unset to disable
  `/create` entirely.

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
- `/project <name>` — switch active project.
- `/projects` — list configured projects.
- `/create <name>` — create a new project folder under `project_root` and
  switch to it (only if `project_root` is configured; flat names only, no
  slashes — SPEC.md §7.2).
- `/new` — reset the active project's Claude Code session.
- `/stop` — cancel the task currently running for the active project.
- `/enable` / `/disable` — allow/stop messages from reaching Claude Code.
- `/status` — enabled/disabled state, active project, busy/idle.
- `/help` — show the command list.

## Repository layout

```
SPEC.md                    full technical specification
src/claude_orchestrator/   Python orchestrator (dispatcher, Claude Code CLI wrapper, transcription, formatting, enable/disable state)
whatsapp-bridge/           Node.js/TypeScript Baileys <-> Redis bridge
config/config.example.yaml config template (copy to ~/.config/claude-orchestrator/config.yaml)
systemd/                   systemd --user unit files
```
