"""Chat command parsing (SPEC.md §7)."""

from __future__ import annotations

from dataclasses import dataclass

HELP_TEXT = (
    "Available commands:\n"
    "/project <name> — switch the active project\n"
    "/projects — list configured projects\n"
    "/create <name> — create a new project folder and switch to it\n"
    "/new — start a fresh session for the active project\n"
    "/stop — cancel the task currently running for the active project\n"
    "/enable — allow messages to reach Claude Code\n"
    "/disable — stop messages from reaching Claude Code (for personal notes/backups)\n"
    "/status — show enabled/disabled state, active project, and busy/idle\n"
    "/help — show this message\n"
    "Anything else is sent to Claude Code as a prompt, unless disabled."
)


@dataclass(frozen=True)
class Command:
    name: str
    arg: str | None = None


def parse_command(text: str) -> Command | None:
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    parts = stripped[1:].split(maxsplit=1)
    if not parts or not parts[0]:
        return None
    name = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else None
    return Command(name=name, arg=arg or None)
