"""Claude Code Remote Control servers: one per project, each in its own
detached tmux session, so the project shows up in the Claude app's Code
tab (SPEC.md §7.7)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pwd
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .config import RemoteControlConfig

logger = logging.getLogger(__name__)

SESSION_PREFIX = "rc-"
CLAUDE_JSON = Path.home() / ".claude.json"

# remote-control prints this once it has registered with claude.ai.
_URL_RE = re.compile(r"https://claude\.ai/code\S*")
_TAIL_LINES = 15


def _target(session: str) -> str:
    """Exact-match tmux target for the session's window: a bare name would
    also match any session it is a prefix of ("rc-app" -> "rc-app2")."""
    return f"={session}:"


def session_name(project_name: str) -> str:
    """tmux session name, also used as the Remote Control name shown in the
    app: "c/whey" -> "rc-c-whey". tmux treats "." and ":" as target
    separators, so every character outside [A-Za-z0-9_-] becomes "-"."""
    return SESSION_PREFIX + re.sub(r"[^A-Za-z0-9_-]", "-", project_name)


def ensure_trusted(path: Path, claude_json: Path = CLAUDE_JSON) -> None:
    """Set the same ~/.claude.json flag Claude Code's trust prompt writes,
    so the server never stops on a prompt it has no one to answer. Writes
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


@dataclass(frozen=True)
class LaunchResult:
    # started: came up during this launch; running: was already up;
    # pending: still no URL after the wait; failed: exited or never started.
    status: Literal["started", "running", "pending", "failed"]
    session: str
    url: str | None = None
    detail: str = ""


class RemoteControl:
    """Starts, finds and stops `claude remote-control` servers in tmux."""

    def __init__(
        self,
        config: RemoteControlConfig,
        binary: str,
        *,
        claude_json: Path = CLAUDE_JSON,
        shell: str | None = None,
        tmux_socket: str | None = None,
        use_scope: bool = True,
        ready_timeout_seconds: float = 20,
        poll_interval_seconds: float = 1,
    ) -> None:
        self._config = config
        self._binary = binary
        self._claude_json = claude_json
        self._shell = shell or login_shell()
        self._tmux_socket = tmux_socket
        # The tmux server must live outside the orchestrator's own systemd
        # unit, or every service restart would kill every server with it.
        self._use_scope = use_scope
        self._ready_timeout = ready_timeout_seconds
        self._poll_interval = poll_interval_seconds

    async def _tmux(self, *args: str, scope: bool = False) -> tuple[int, str]:
        socket = ["-L", self._tmux_socket] if self._tmux_socket else []
        cmd = ["tmux", *socket, *args]
        if scope and self._use_scope:
            cmd = ["systemd-run", "--user", "--scope", "--quiet", *cmd]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
            )
        except FileNotFoundError as exc:
            return 127, f"{exc.filename} not found"
        out, _ = await proc.communicate()
        return proc.returncode or 0, out.decode(errors="replace")

    async def _pane_state(self, session: str) -> Literal["absent", "live", "dead"]:
        code, out = await self._tmux("list-panes", "-t", _target(session), "-F", "#{pane_dead}")
        if code != 0:
            return "absent"
        return "dead" if out.strip() == "1" else "live"

    async def _capture(self, session: str) -> str:
        _, out = await self._tmux("capture-pane", "-p", "-J", "-S", "-200", "-t", _target(session))
        return out

    @staticmethod
    def _tail(text: str) -> str:
        lines = [line for line in text.splitlines() if line.strip()]
        return "\n".join(lines[-_TAIL_LINES:])

    async def running(self) -> list[str]:
        """Names of the live Remote Control tmux sessions, sorted."""
        code, out = await self._tmux("list-sessions", "-F", "#{session_name} #{pane_dead}")
        if code != 0:
            return []  # no tmux server, so no sessions
        sessions = (line.rsplit(" ", 1) for line in out.splitlines() if " " in line)
        return sorted(name for name, dead in sessions if name.startswith(SESSION_PREFIX) and dead == "0")

    async def launch(self, project_name: str, path: Path) -> LaunchResult:
        """Start the project's server unless it is already live, then wait
        for the claude.ai URL it prints once ready."""
        session = session_name(project_name)
        state = await self._pane_state(session)
        if state == "live":
            match = _URL_RE.search(await self._capture(session))
            return LaunchResult("running", session, url=match.group(0) if match else None)
        if state == "dead":
            await self._tmux("kill-session", "-t", f"={session}")

        try:
            await asyncio.to_thread(ensure_trusted, path, self._claude_json)
        except (OSError, ValueError) as exc:
            logger.exception("could not mark %s as trusted", path)
            return LaunchResult("failed", session, detail=f"could not mark the folder as trusted: {exc}")

        code, out = await self._tmux(
            "new-session", "-d", "-s", session, "-x", "200", "-y", "50", "-c", str(path),
            # The server runs in an interactive login shell, so it and its
            # sessions get the same environment as a terminal (PATH, nvm,
            # ~/.bashrc), not the orchestrator service's. LD_LIBRARY_PATH is
            # the service's own CUDA setting and is dropped. The arguments
            # reach the binary as "$0" "$@", never parsed by the shell.
            "env", "-u", "LD_LIBRARY_PATH", self._shell, "-lic", 'exec "$0" "$@"',
            self._binary, "remote-control", "--name", session,
            "--spawn", self._config.spawn, "--permission-mode", self._config.permission_mode,
            "--chrome" if self._config.chrome else "--no-chrome",
            # Keep the pane after the process exits, so a failure's output
            # can still be read.
            ";", "set-option", "-t", _target(session), "remain-on-exit", "on",
            scope=True,
        )  # fmt: skip
        if code != 0:
            return LaunchResult("failed", session, detail=out.strip())
        return await self._wait_ready(session)

    async def _wait_ready(self, session: str) -> LaunchResult:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._ready_timeout
        text = ""
        while loop.time() < deadline:
            await asyncio.sleep(self._poll_interval)
            state = await self._pane_state(session)
            text = await self._capture(session)
            if match := _URL_RE.search(text):
                return LaunchResult("started", session, url=match.group(0))
            if state != "live":
                await self._tmux("kill-session", "-t", f"={session}")
                return LaunchResult("failed", session, detail=self._tail(text))
        return LaunchResult("pending", session, detail=self._tail(text))

    async def close(self, project_name: str) -> bool:
        """Kill the project's server. False when none was running."""
        code, _ = await self._tmux("kill-session", "-t", f"={session_name(project_name)}")
        return code == 0
