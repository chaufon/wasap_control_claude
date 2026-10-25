# Architecture

## Components

- **Bridge** (`bridge/`, Node, Baileys): a linked WhatsApp device. Relays your self-chat to and from the orchestrator. `--pair` pairs and prints `PAIRED <jid>`.
- **Orchestrator** (`control/`, Python): owns the socket, runs commands, transcribes voice notes, types prompts into live sessions and sends the answers.
- **Live tmux sessions**: one `claude --remote-control` per project in tmux session `rc-<project>`. WhatsApp, the Claude app and `tmux attach` drive the same process.
- **Hook relay** (`wasap_control.hook_relay`): each session's Claude Code hook. Sends one `hook` frame per event; answers go to WhatsApp only for prompts typed from WhatsApp.

## Socket protocol

- Unix socket, mode `0600`: `transport.socket_path`, else `$XDG_RUNTIME_DIR/wasap-control.sock`, else `${TMPDIR:-/tmp}/wasap-control-<uid>.sock`.
- One JSON object per line: `{"kind": ..., "data": {...}}`. Kinds: `hello` (bridge, first on each connect, `{"role": "bridge"}`), `incoming` (bridge: a WhatsApp message), `outgoing` (orchestrator, to every bridge: text or a media file), `hook` (relay: one event, then disconnect). Frames sent while the other side is down are dropped.

## Config

- `~/.config/wasap-control/config.yaml` (or `$WASAP_CONTROL_CONFIG`); template `deploy/config.example.yaml`. Sections: `whatsapp`, `projects`, `project_root`, `project_containers`, `claude_code`, `remote_control`, `whisper`, `tts`, `transport`, `progress_updates`, `bridge`.

## State and data locations

- `~/.local/share/wasap-control-bridge/auth/`: WhatsApp pairing, mode 0700, written atomically. `~/.local/share/wasap-control/`: `state.json` (toggles, active project, session IDs, last answers), `live/` (WhatsApp prompt markers), `logs/`, `piper-voices/`, and `node/` (private Node, only without system Node >= 20).
- `<project>/whatsapp-inbox/`, `<project>/whatsapp-outbox/`: files from and to WhatsApp.

## Services

- `wasap-control.service` and `wasap-control-bridge.service`: user units rendered by `install.sh` from `deploy/systemd/*.service.in`. The bridge starts after the orchestrator. Linger is on unless `--login-only`.
- `RestartPreventExitStatus=78`: the orchestrator exits 78 on a configuration error (missing, unreadable or invalid `config.yaml`), when `claude`, `tmux` or `ffmpeg` is missing, on `bypassPermissions` as root, or when the socket path is taken or not writable. The bridge exits 78 on a configuration error and when WhatsApp logs the device out. systemd does not restart either. After 10 failed WhatsApp reconnects the bridge exits 0 and stays stopped.
