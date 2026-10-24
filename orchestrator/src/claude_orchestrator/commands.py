"""Chat command parsing and the /help text."""

from __future__ import annotations

from dataclasses import dataclass

HELP_TEXT = """📋 *Commands*

*Projects*
• /projects — list every project, numbered, grouped by container
• /project <name-or-number> — switch to a project and start its shared session
• /create <name> | <container>/<name> — create a project folder and switch to it
• /close [name-or-number] — stop a project's shared session (default: the active one)

*Session*
• /last — Claude's last answer in the active project, from any device
• /stop — drop queued messages and interrupt the running turn
• /status — state, active project, and every Remote Control session
• /reconnect [name-or-number] — connect a session to the Claude app again

*Settings*
• /enable — let messages reach Claude Code
• /disable — keep messages away from Claude Code (personal notes)
• /voice on | off — also send replies as a voice note

*Pass-through*
• /c <text> — send <text> to the session as typed, e.g. /c /usage, /c /clear

Anything else goes to the active project's session, unless disabled. The same session is open in the Claude app and on the computer."""


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
