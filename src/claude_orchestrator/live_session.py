"""Shared Claude Code sessions: one interactive `claude --remote-control`
per project, each in its own detached tmux session. The Claude app (Remote
Control), the computer (`tmux attach`) and WhatsApp all drive that one live
process, so they share one conversation (SPEC.md §7.7)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pwd
import re
import shlex
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .config import RemoteControlConfig
from .outbox import OUTBOX_SYSTEM_PROMPT

logger = logging.getLogger(__name__)

SESSION_PREFIX = "rc-"
CLAUDE_JSON = Path.home() / ".claude.json"
# Claude Code writes one <pid>.json per running process here, with its
# session ID, busy/idle status, tmux target and, while Remote Control is
# connected, its claude.ai session ID. Undocumented, so every field is read
# defensively.
CLAUDE_SESSIONS_DIR = Path.home() / ".claude" / "sessions"
LIVE_DIR = Path.home() / ".local" / "share" / "claude-orchestrator" / "live"

# The hook events the relay forwards to the orchestrator (hook_relay.py).
HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "PostToolUse", "Stop")

_TAIL_LINES = 15
_RULE_RE = re.compile(r"\s*[─━▔▁]+\s*")

PaneState = Literal["absent", "live", "dead"]


def _target(session: str) -> str:
    """Exact-match tmux target for the session's window: a bare name would
    also match any session it is a prefix of ("rc-app" -> "rc-app2")."""
    return f"={session}:"


def session_name(project_name: str) -> str:
    """tmux session name, also used as the session name shown in the app:
    "c/whey" -> "rc-c-whey". tmux treats "." and ":" as target separators,
    so every character outside [A-Za-z0-9_-] becomes "-"."""
    return SESSION_PREFIX + re.sub(r"[^A-Za-z0-9_-]", "-", project_name)


def ensure_trusted(path: Path, claude_json: Path = CLAUDE_JSON) -> None:
    """Set the same ~/.claude.json flag Claude Code's trust prompt writes,
    so the session never stops on a prompt it has no one to answer. Writes
    only when the flag is missing or false, through a temp file + rename so
    no reader ever sees a half-written file."""
    data = json.loads(claude_json.read_text(encoding="utf-8")) if claude_json.exists() else {}
    entry = data.setdefault("projects", {}).setdefault(str(path.resolve()), {})
    if entry.get("hasTrustDialogAccepted") is True:
        return
    entry["hasTrustDialogAccepted"] = True
    fd, tmp = tempfile.mkstemp(dir=claude_json.parent, prefix=".claude.json.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, claude_json)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    logger.info("marked %s as trusted in %s", path, claude_json)


def login_shell() -> str:
    """The owner's login shell from the password database."""
    return pwd.getpwuid(os.getuid()).pw_shell or "/bin/sh"


def hook_settings(relay_command: list[str], session: str, live_dir: Path) -> str:
    """Inline `--settings` JSON that sends every HOOK_EVENTS event of this
    session to the relay, tagged with the tmux session name, along with the
    folder of its pending markers."""
    command = shlex.join([*relay_command, "--session", session, "--live-dir", str(live_dir)])
    entry = [{"hooks": [{"type": "command", "command": command}]}]
    return json.dumps({"hooks": {event: entry for event in HOOK_EVENTS}})


def pending_path(live_dir: Path, session: str) -> Path:
    """Marker the orchestrator writes right before typing a WhatsApp message
    into the session; the relay consumes it on the next UserPromptSubmit to
    tag that turn as WhatsApp's (hook_relay.py)."""
    return live_dir / f"{session}.pending.json"


@dataclass(frozen=True)
class ProcessInfo:
    """One running Claude Code process, from CLAUDE_SESSIONS_DIR."""

    pid: int
    session_id: str
    cwd: Path
    busy: bool
    bridge_session_id: str | None
    tmux_session: str | None

    @property
    def url(self) -> str | None:
        """The session's claude.ai link while Remote Control is connected."""
        return f"https://claude.ai/code/{self.bridge_session_id}" if self.bridge_session_id else None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_processes(sessions_dir: Path = CLAUDE_SESSIONS_DIR) -> list[ProcessInfo]:
    """Every live Claude Code process on this machine. Files of exited
    processes and files that don't parse are skipped."""
    found: list[ProcessInfo] = []
    for path in sessions_dir.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            pid = int(data["pid"])
            info = ProcessInfo(
                pid=pid,
                session_id=str(data.get("sessionId") or ""),
                cwd=Path(data.get("cwd") or "/"),
                busy=data.get("status") == "busy",
                bridge_session_id=data.get("bridgeSessionId") or None,
                tmux_session=(str(data["tmux"]).split(":", 1)[0] if data.get("tmux") else None),
            )
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if _alive(pid):
            found.append(info)
    return found


def screen_text(capture: str, command: str | None = None) -> str:
    """The useful part of a captured screen. A full-screen panel (/usage,
    /cost) is everything below its "▔" top border; otherwise it is what
    follows the echo of `command`. Either way it ends at the input box, and
    blank lines are dropped."""
    lines = [line.rstrip() for line in capture.splitlines()]
    borders = [i for i, line in enumerate(lines) if line.lstrip().startswith("▔")]
    if borders:
        lines = lines[borders[-1] + 1 :]
    elif command:
        echoes = [i for i, line in enumerate(lines) if line.startswith("❯") and line[1:].strip().startswith(command)]
        if echoes:
            lines = lines[echoes[-1] + 1 :]
    kept: list[str] = []
    for line in lines:
        if not line.strip():
            continue
        if line.startswith("❯") or _RULE_RE.fullmatch(line):
            if kept:
                break
            continue
        # A panel marks that it scrolls with a "↓" at the right edge.
        kept.append(line.removesuffix("↓").rstrip())
    return "\n".join(kept)


@dataclass(frozen=True)
class LaunchResult:
    # started: came up during this launch; running: was already up;
    # pending: up, but Remote Control not connected after the wait;
    # failed: exited or never started.
    status: Literal["started", "running", "pending", "failed"]
    session: str
    url: str | None = None
    detail: str = ""


class LiveSessions:
    """Starts, finds, drives and stops the per-project live sessions in tmux."""

    def __init__(
        self,
        config: RemoteControlConfig,
        binary: str,
        relay_command: list[str],
        *,
        claude_json: Path = CLAUDE_JSON,
        sessions_dir: Path = CLAUDE_SESSIONS_DIR,
        live_dir: Path = LIVE_DIR,
        shell: str | None = None,
        tmux_socket: str | None = None,
        use_scope: bool = True,
        ready_timeout_seconds: float = 30,
        poll_interval_seconds: float = 1,
        paste_settle_seconds: float = 0.5,
        escape_settle_seconds: float = 1.0,
    ) -> None:
        self._config = config
        self._binary = binary
        self._relay_command = relay_command
        self._claude_json = claude_json
        self._sessions_dir = sessions_dir
        self._live_dir = live_dir
        self._shell = shell or login_shell()
        self._tmux_socket = tmux_socket
        # The tmux server must live outside the orchestrator's own systemd
        # unit, or every service restart would kill every session with it.
        self._use_scope = use_scope
        self._ready_timeout = ready_timeout_seconds
        self._poll_interval = poll_interval_seconds
        self._paste_settle = paste_settle_seconds
        self._escape_settle = escape_settle_seconds

    @property
    def poll_interval(self) -> float:
        return self._poll_interval

    async def _tmux(self, *args: str, scope: bool = False, stdin: bytes | None = None) -> tuple[int, str]:
        socket = ["-L", self._tmux_socket] if self._tmux_socket else []
        cmd = ["tmux", *socket, *args]
        if scope and self._use_scope:
            cmd = ["systemd-run", "--user", "--scope", "--quiet", *cmd]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE if stdin is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError as exc:
            return 127, f"{exc.filename} not found"
        out, _ = await proc.communicate(stdin)
        return proc.returncode or 0, out.decode(errors="replace")

    async def pane_state(self, session: str) -> PaneState:
        code, out = await self._tmux("list-panes", "-t", _target(session), "-F", "#{pane_dead}")
        if code != 0:
            return "absent"
        return "dead" if out.strip() == "1" else "live"

    async def capture(self, session: str, history: bool = False) -> str:
        extra = ["-S", "-200"] if history else []
        _, out = await self._tmux("capture-pane", "-p", "-J", *extra, "-t", _target(session))
        return out

    @staticmethod
    def _tail(text: str) -> str:
        lines = [line for line in text.splitlines() if line.strip()]
        return "\n".join(lines[-_TAIL_LINES:])

    async def last_line(self, session: str) -> str:
        """The last line a pane's process printed, e.g. a dead session's
        error, skipping tmux's own "Pane is dead" notice."""
        lines = [line.strip() for line in (await self.capture(session, history=True)).splitlines()]
        return next((line for line in reversed(lines) if line and not line.startswith("Pane is dead")), "")

    def process(self, session: str) -> ProcessInfo | None:
        """The Claude Code process running in this tmux session, if any."""
        return next((p for p in read_processes(self._sessions_dir) if p.tmux_session == session), None)

    def all_processes(self) -> list[ProcessInfo]:
        return read_processes(self._sessions_dir)

    async def sessions(self) -> dict[str, PaneState]:
        """Every Remote Control tmux session, live or dead, by name."""
        code, out = await self._tmux("list-sessions", "-F", "#{session_name} #{pane_dead}")
        if code != 0:
            return {}  # no tmux server, so no sessions
        found: dict[str, PaneState] = {}
        for line in out.splitlines():
            name, _, dead = line.rpartition(" ")
            if name.startswith(SESSION_PREFIX):
                found[name] = "dead" if dead == "1" else "live"
        return dict(sorted(found.items()))

    async def launch(self, project_name: str, path: Path, resume_session_id: str | None = None) -> LaunchResult:
        """Start the project's live session unless it is already up, resuming
        `resume_session_id` when given, then wait for Remote Control to
        connect."""
        session = session_name(project_name)
        state = await self.pane_state(session)
        if state == "live":
            info = self.process(session)
            return LaunchResult("running", session, url=info.url if info else None)
        if state == "dead":
            await self._tmux("kill-session", "-t", f"={session}")

        try:
            await asyncio.to_thread(ensure_trusted, path, self._claude_json)
        except (OSError, ValueError) as exc:
            logger.exception("could not mark %s as trusted", path)
            return LaunchResult("failed", session, detail=f"could not mark the folder as trusted: {exc}")

        claude_args = [
            self._binary, "--remote-control", session,
            "--permission-mode", self._config.permission_mode,
            "--chrome" if self._config.chrome else "--no-chrome",
            "--settings", hook_settings(self._relay_command, session, self._live_dir),
            "--append-system-prompt", OUTBOX_SYSTEM_PROMPT,
        ]  # fmt: skip
        if resume_session_id:
            claude_args += ["--resume", resume_session_id]
        code, out = await self._tmux(
            "new-session", "-d", "-s", session, "-x", "200", "-y", "50", "-c", str(path),
            # The session runs in an interactive login shell, so it gets the
            # same environment as a terminal (PATH, nvm, ~/.bashrc), not the
            # orchestrator service's. LD_LIBRARY_PATH is the service's own
            # CUDA setting and is dropped. The arguments reach the binary as
            # "$0" "$@", never parsed by the shell.
            "env", "-u", "LD_LIBRARY_PATH", self._shell, "-lic", 'exec "$0" "$@"', *claude_args,
            # Keep the pane after the process exits, so a failure's output
            # can still be read.
            ";", "set-option", "-t", _target(session), "remain-on-exit", "on",
            scope=True,
        )  # fmt: skip
        if code != 0:
            return LaunchResult("failed", session, detail=out.strip())
        return await self._wait_ready(session)

    async def _wait_ready(self, session: str) -> LaunchResult:
        deadline = time.monotonic() + self._ready_timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(self._poll_interval)
            if await self.pane_state(session) != "live":
                text = await self.capture(session, history=True)
                await self._tmux("kill-session", "-t", f"={session}")
                return LaunchResult("failed", session, detail=self._tail(text))
            info = self.process(session)
            if info is not None and info.url:
                return LaunchResult("started", session, url=info.url)
        return LaunchResult("pending", session, detail=self._tail(await self.capture(session)))

    async def wait_registered(self, session: str, timeout_seconds: float) -> ProcessInfo | None:
        """Wait until the session's process has registered itself, which
        means it is ready for input."""
        deadline = time.monotonic() + timeout_seconds
        while True:
            info = self.process(session)
            if info is not None or time.monotonic() >= deadline:
                return info
            if await self.pane_state(session) != "live":
                return None
            await asyncio.sleep(self._poll_interval)

    def mark_pending(self, session: str, voice: bool) -> None:
        """Tag the next prompt of this session as WhatsApp's (pending_path)."""
        path = pending_path(self._live_dir, session)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"voice": voice, "at": time.time()}), encoding="utf-8")

    def clear_pending(self, session: str) -> None:
        pending_path(self._live_dir, session).unlink(missing_ok=True)

    async def send_text(self, session: str, text: str) -> bool:
        """Type `text` into the session, then press Enter once. A single line
        is typed key by key, so a slash command goes through the command
        menu as if typed by hand; several lines go in as one bracketed paste,
        so their line breaks never submit early."""
        if "\n" not in text:
            code, _ = await self._tmux("send-keys", "-t", _target(session), "-l", text)
            if code != 0:
                return False
            await asyncio.sleep(self._paste_settle)
            return await self.send_key(session, "Enter")
        buffer = f"wa-{session}"
        code, _ = await self._tmux("load-buffer", "-b", buffer, "-", stdin=text.encode())
        if code != 0:
            return False
        code, _ = await self._tmux("paste-buffer", "-p", "-d", "-b", buffer, "-t", _target(session))
        if code != 0:
            return False
        await asyncio.sleep(self._paste_settle)
        return await self.send_key(session, "Enter")

    async def send_key(self, session: str, key: str) -> bool:
        code, _ = await self._tmux("send-keys", "-t", _target(session), key)
        if key == "Escape":
            # A key sent right after Escape would arrive as Alt+key and be
            # lost, so the next keys wait.
            await asyncio.sleep(self._escape_settle)
        return code == 0

    async def close(self, project_name: str) -> bool:
        """Kill the project's live session. False when none was running."""
        return await self.kill(session_name(project_name))

    async def kill(self, session: str) -> bool:
        code, _ = await self._tmux("kill-session", "-t", f"={session}")
        return code == 0
