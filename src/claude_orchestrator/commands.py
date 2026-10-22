"""Chat command parsing (SPEC.md §7)."""

from __future__ import annotations

from dataclasses import dataclass

HELP_TEXT = (
    "Available commands:\n"
    "/project <name-or-number> — switch the active project and start its "
    "Remote Control server, so it shows up in the Claude app's Code tab\n"
    "/projects — list every available project (config.yaml plus every "
    "folder under project_root), numbered, grouped by container\n"
    "/create <name> | <container>/<name> — create a new project folder, "
    "switch to it and start its Remote Control server\n"
    "/close [name-or-number] — stop a project's Remote Control server "
    "(the active project's if none given)\n"
    "/new — start a fresh session for the active project\n"
    "/stop — cancel the task currently running for the active project\n"
    "/enable — allow messages to reach Claude Code\n"
    "/disable — stop messages from reaching Claude Code (for personal notes/backups)\n"
    "/status — show enabled/disabled state, active project, busy/idle, "
    "session ID, and running Remote Control servers\n"
    "/c <text> — send <text> to Claude Code verbatim, e.g. \"/c /usage\" or "
    "\"/c /grill-me an idea\" (bypasses the commands above)\n"
    "/voice on | off — also send replies as a voice note, in the reply's own "
    "detected language (off by default)\n"
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
