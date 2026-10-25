# Claude Code Remote Control via WhatsApp

Control Claude Code on your computer from WhatsApp. You write to your own
"Message Yourself" chat: text, voice notes or files. Each message goes to a
Claude Code session for the active project, and the answer comes back to the
chat. The same session is open in the Claude app (Remote Control) and in a
terminal on your computer (`tmux attach`).

Two processes run as systemd user services: a Node bridge linked to WhatsApp,
and a Python orchestrator that runs the Claude Code sessions. See
[`docs/architecture.md`](docs/architecture.md) for how they fit together.

## Requirements

- Linux with systemd.
- A Claude subscription. The sessions use your Claude Code login, never an
  API key.
- A phone with WhatsApp.

The installer gets everything else: `ffmpeg`, `tmux` and `curl` from your
distro (`apt`, `dnf`, `pacman` or `zypper`), Node 20 or newer, `uv`, Claude
Code, and the Python and Node dependencies. With a working `nvidia-smi` it
also installs the CUDA libraries, so voice notes are transcribed on the GPU.

Turn off automatic suspend: the services cannot answer while the computer
sleeps.

## Install

```bash
git clone https://github.com/chaufon/wasap_control_claude.git
cd wasap_control_claude
./install.sh
```

Run it as your normal user. The script needs root only to install missing
system packages, and to turn linger on or off when `loginctl` refuses to do it
without root. In both cases it prints the exact `sudo ./install.sh` command to
run and exits with code 3. Under `sudo`, everything except the system packages and linger
runs as the user who called `sudo`. Plain root (no `sudo`) installs for root,
with `permission_mode: auto`.

The script:

1. Installs missing system packages, Node, `uv` and Claude Code. If Claude
   Code is not logged in, it runs `claude auth login`.
2. Installs the Python and Node dependencies and builds the bridge.
3. Asks for the folder that holds your projects (`project_root`, default
   `~/projects`) and writes `~/.config/claude-orchestrator/config.yaml`. An
   existing config is kept; the script lists keys that are missing from it
   or obsolete.
4. Pairs WhatsApp when no sender is configured: scan the QR code with
   WhatsApp > Linked devices. Your number is written to the config.
5. Turns on linger, so the services run from boot.
6. Writes, enables and starts `claude-orchestrator.service` and
   `whatsapp-bridge.service`.

Run `./install.sh` again after `git pull`. It keeps the config and the
pairing, rewrites the units and restarts the services.

## Flags

```
--check       Report what would be done; render the config and the units
              into a temp folder and print their paths. Writes no config,
              no units, runs no systemctl or loginctl, installs no Python or
              Node dependencies and does not pair. As root it still installs
              missing system packages, Node, uv and Claude Code, because
              they are needed to check the rest; as a normal user it only
              reports them.
--login-only  Run the services only while the user is logged in (disables
              linger). By default linger is enabled, so the services run
              from boot.
--uninstall   Stop and disable both services and delete their unit files.
              Keeps the config and the data folders.
--purge       Implies --uninstall. After one [y/N] confirmation, also
              deletes ~/.config/claude-orchestrator,
              ~/.local/share/whatsapp-bridge and
              ~/.local/share/claude-orchestrator.
--help        Show this help.
```

Exit codes: `0` done, `2` bad flag, `3` root is needed (run the printed
`sudo` command), `4` unsupported system (install the printed packages
yourself), `78` a check or a step failed. `--uninstall` and `--purge` cannot
be combined with `--check` or `--login-only` (exit code 2).

## Configure

Edit `~/.config/claude-orchestrator/config.yaml`, then restart both services:

```bash
systemctl --user restart claude-orchestrator.service whatsapp-bridge.service
```

`deploy/config.example.yaml` explains every key. The main ones:

- `project_root`: every non-hidden folder directly under it is a project.
  `/create` makes new project folders here. Leave it unset to use only
  `projects` and turn off `/create`.
- `project_containers`: folders under `project_root` (e.g. `c`, `c/car`)
  that hold projects instead of being one. Their projects are named by
  relative path (`c/whey`, `c/car/maintenance`).
- `projects`: extra projects as `{name, path}` pairs, anywhere on disk.
- `remote_control.permission_mode`: how the sessions ask for permission.
  `bypassPermissions` never asks and cannot run as root; `auto` asks only
  about actions its safety check flags.
- `remote_control.chrome`: Claude in Chrome for the sessions (default
  `false`: browser work uses the Playwright MCP plugin).
- `whisper`: speech-to-text model and device (`large-v3` on `cuda` with a
  GPU, `medium` on `cpu` without).
- `tts.voices`: Piper voice per reply language for `/voice on`. Other
  languages use `en`.
- `whatsapp.allowed_sender_jid`: your number, written by pairing. Use the
  phone-number form (`...@s.whatsapp.net`).

Logs:

```bash
journalctl --user -u claude-orchestrator.service -f
journalctl --user -u whatsapp-bridge.service -f
```

The orchestrator also writes `~/.local/share/claude-orchestrator/logs/orchestrator.log`.

If the orchestrator exits with code 78 (a configuration error such as a
missing or invalid `config.yaml`, missing `claude`, `tmux` or `ffmpeg`,
`bypassPermissions` as root, or a socket path that is taken or not writable),
systemd does not restart it. The bridge also exits 78 on a configuration error
and when WhatsApp logs the device out. Fix the cause in the log, then restart
the service.

After 10 failed reconnects to WhatsApp the bridge exits, and systemd does not
restart it. Start it again with `systemctl --user start whatsapp-bridge.service`
once the network is back.

To pair again (for example after WhatsApp logged the device out):

1. Stop the bridge: `systemctl --user stop whatsapp-bridge.service`. The
   service and `--pair` must never use the auth folder at the same time.
2. Move the auth folder aside:
   `mv ~/.local/share/whatsapp-bridge/auth ~/.local/share/whatsapp-bridge/auth.old`.
   Delete `auth.old` once the new pairing works.
3. In `bridge/`, run `node dist/index.js --pair` with the Node binary from the
   `ExecStart=` line of `whatsapp-bridge.service`. Scan the QR code. It prints
   `PAIRED <jid>` and does not change the config.
4. If you paired a different number, set `whatsapp.allowed_sender_jid` to the
   printed `<jid>`, or run
   `orchestrator/.venv/bin/python -m claude_orchestrator.install_config set-sender --path ~/.config/claude-orchestrator/config.yaml --jid <jid>`.
5. Restart both services:
   `systemctl --user restart claude-orchestrator.service whatsapp-bridge.service`.

## Using it

Write in your "Message Yourself" chat.

- Text, voice notes and files go to the active project's session. A voice
  note is transcribed first, and you get the transcript back ("🎙️ Heard: …"). A file
  is saved into `<project>/whatsapp-inbox/` and Claude Code is told where.
- **One session, three devices.** Each project has one live Claude Code
  session in tmux (`rc-<project>`). WhatsApp, the Claude app (Code tab) and
  `tmux attach -t rc-<project>` on your computer all drive it. Only answers to
  messages sent from WhatsApp come back to WhatsApp. Sessions survive
  orchestrator restarts and resume their last conversation when started
  again.
- A message sent while the session is busy is queued ("⏳ Busy, queued").
- Replying to an earlier message (WhatsApp's quote feature) adds the quoted
  text, file or voice note as context.
- To send you a file, Claude Code saves it into `<project>/whatsapp-outbox/`.
  It knows this convention, so you can ask "send me the report". Files are
  sent after the reply and then moved to `whatsapp-outbox/sent/`. Files over
  64 MB are refused with an error.

Commands:

- `/projects`: list every project, numbered, grouped by container.
- `/project <name-or-number>`: switch to a project and start its session.
  The reply has the Claude app link, the `tmux attach` command and a preview
  of the last answer. `/project <container>` lists that container's projects.
- `/create <name>` or `/create <container>/<name>`: create a project folder
  under `project_root` and switch to it. Names allow letters, numbers, `-`
  and `_`.
- `/close [name-or-number]`: stop a project's session (default: the active
  one).
- `/last`: Claude's full last answer in the active project, from any device.
- `/stop`: drop queued messages and interrupt the running turn.
- `/status`: enabled or disabled, voice replies, the active project and every
  Remote Control session (🟢 connected, 🟡 not connected, 🔴 exited, ⚪ started
  outside WhatsApp).
- `/reconnect [name-or-number]`: connect a session to the Claude app again,
  for example after you archived it there.
- `/enable` and `/disable`: let messages reach Claude Code, or keep them away.
  While disabled, the chat is for personal notes: text, voice notes and files
  are left alone, and any `/` command other than the ones above replies that
  Claude Code is disabled.
- `/voice on` and `/voice off`: also send each answer as a voice note (off by
  default). The voice note speaks a short summary; the text reply has the
  full answer. Its language (English or Spanish) is detected from the reply.
- `/c <text>`: type `<text>` into the session as is, to reach Claude Code's
  own commands and skills, e.g. `/c /usage` or `/c /clear` (starts a fresh
  conversation).
- `/help`: show the command list.

## Uninstall

```bash
./install.sh --uninstall   # remove the services, keep config and data
./install.sh --purge       # also delete config, WhatsApp pairing and state
```

Linger stays on. Turn it off with `loginctl disable-linger $USER` if no other
service needs it.

## Development

```
orchestrator/            Python orchestrator (src/claude_orchestrator, tests)
bridge/                  Node/TypeScript WhatsApp bridge (src, test)
install.sh               installer
deploy/install/          installer functions
deploy/systemd/          unit templates
deploy/config.example.yaml
deploy/tests/            installer tests and container tests
docs/architecture.md     components, socket protocol, config, data, services
```

Tests:

```bash
cd orchestrator && uv run pytest -q                       # without a GPU
cd orchestrator && uv run --extra cuda pytest -q          # with the cuda extra
uv run --project orchestrator pytest deploy/tests -q      # installer, from the repo root (add --extra cuda with a GPU)
cd bridge && npm test && npm run build                    # bridge
deploy/tests/containers.sh                                # Ubuntu, Fedora, Arch and Node 20 containers; needs Docker
```

On a machine where `install.sh` installed the `cuda` extra, always pass
`--extra cuda` to `uv run` and `uv sync`. A plain `uv run pytest` syncs the
venv without the extra and removes the NVIDIA libraries from the venv the
service uses, which breaks GPU Whisper until the next `uv sync --extra cuda`.

After changing code, run `./install.sh` again or restart both services.
