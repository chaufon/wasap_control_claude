# Architecture

## Components

- **Bridge** (`bridge/`, Node/TypeScript, Baileys). Linked to your WhatsApp account as a device. It reads only your own "Message Yourself" chat, saves voice notes and files to `bridge.staging_dir`, and sends each message to the orchestrator. It sends the orchestrator's replies back to that chat. `dist/index.js --pair` shows a QR code, pairs, and prints `PAIRED <jid>`.
- **Orchestrator** (`orchestrator/`, Python). Owns the socket. Handles commands, keeps the active project, transcribes voice notes (faster-whisper), moves files into `<project>/whatsapp-inbox/`, types prompts into the project's live session, and sends answers back. Optional voice replies use Piper and `ffmpeg`.
- **Live tmux sessions**. One interactive `claude --remote-control rc-<project>` per project, in a detached tmux session named `rc-<project>`, started through your login shell. WhatsApp, the Claude app (Remote Control) and `tmux attach -t rc-<project>` all drive the same process. Files Claude Code saves into `<project>/whatsapp-outbox/` are sent to WhatsApp and moved to `whatsapp-outbox/sent/`.
- **Hook relay** (`python -m claude_orchestrator.hook_relay`). Each live session runs it as its Claude Code hook for `SessionStart`, `UserPromptSubmit`, `PostToolUse` and `Stop`. It sends the event to the socket as one `hook` frame, tagged with the tmux session. A prompt typed from WhatsApp is tagged `"origin": "whatsapp"`; only answers to those prompts go to WhatsApp. The relay always exits 0.

## Socket protocol

- Unix socket, mode `0600`. Path: `transport.socket_path`, else `$XDG_RUNTIME_DIR/claude-orchestrator.sock`, else `${TMPDIR:-/tmp}/claude-orchestrator-<uid>.sock`.
- One JSON object per line: `{"kind": ..., "data": {...}}`.

| kind | sender | data |
|---|---|---|
| `hello` | bridge, first frame on each connect | `{"role": "bridge"}` |
| `incoming` | bridge | one WhatsApp message: id, timestamp, sender, type (`text`, `voice`, `attachment`), text or media path, quoted content |
| `outgoing` | orchestrator, to every bridge | text, or a media path with MIME type, file name, caption and `delete_after_send` |
| `hook` | hook relay, one frame then disconnect | the hook event fields and `live_session` |

- Frames sent while the other side is not connected are dropped. Malformed lines are logged and skipped. The bridge reconnects to the socket on its own.
- On start the orchestrator replaces a stale socket file. If another orchestrator listens on the path, it exits 78.

## Config

- File: `~/.config/claude-orchestrator/config.yaml`, or `$CLAUDE_ORCHESTRATOR_CONFIG`. Both processes read it. Template: `deploy/config.example.yaml`. `install.sh` writes it and never overwrites it.
- Sections: `whatsapp` (`allowed_sender_jid`, required), `projects`, `project_root`, `project_containers` (at least `projects` or `project_root`), `claude_code` (`binary`, `task_timeout_seconds`), `remote_control` (`permission_mode`, `chrome`), `whisper`, `tts`, `transport` (`socket_path`), `progress_updates`, `bridge` (`auth_dir`, `staging_dir`, `outgoing_staging_dir`).

## State and data locations

| Path | Content |
|---|---|
| `~/.config/claude-orchestrator/config.yaml` | config |
| `~/.local/share/whatsapp-bridge/auth/` | WhatsApp pairing; each file is written to a temp file, fsynced and renamed |
| `~/.local/share/claude-orchestrator/state.json` | enabled/disabled, voice on/off, active project, session ID and last answer per project |
| `~/.local/share/claude-orchestrator/live/` | pending markers for prompts typed from WhatsApp |
| `~/.local/share/claude-orchestrator/logs/orchestrator.log` | orchestrator log (rotated, 5 x 10 MB) |
| `~/.local/share/claude-orchestrator/piper-voices/` | Piper voice models |
| `~/.local/share/claude-orchestrator/node/` | private Node, only when the system has no Node >= 20 |
| `/tmp/whatsapp-bridge/incoming`, `/tmp/whatsapp-bridge/outgoing` | media in transit |
| `<project>/whatsapp-inbox/` | files received from WhatsApp |
| `<project>/whatsapp-outbox/` | files to send; holds a `.gitignore` with `*`, sent files move to `sent/` |

## Services

- Two systemd user units, rendered by `install.sh` from `deploy/systemd/*.service.in` into `~/.config/systemd/user/` with absolute paths: `claude-orchestrator.service` and `whatsapp-bridge.service`. The bridge starts after the orchestrator.
- Linger is on by default, so the services run from boot. `install.sh --login-only` turns it off, so they run only while you are logged in.
- Both units use `Restart=on-failure` and `RestartPreventExitStatus=78`. The orchestrator exits 78 (`EX_CONFIG`) when `claude_code.binary`, `tmux` or `ffmpeg` is missing, when `permission_mode` is `bypassPermissions` and it runs as root, or when the socket path is taken. systemd does not restart it after 78.
- On `SIGTERM` the bridge waits for pending auth writes before it exits. After 10 failed WhatsApp reconnects it exits 0 and stays stopped.
