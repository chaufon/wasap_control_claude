"""Persisted orchestrator state: on/off toggle, voice replies, active
project, and each project's Claude Code session id.

The self-chat is also used for personal notes/backups (not just Claude Code
commands), so routing is gated by this toggle rather than being always-on.
Persisted across restarts so a deliberate /disable, /voice, /project switch,
or in-progress conversation survives a service restart; a fresh install with
no state file yet defaults to enabled, voice off, no active project
override, and no remembered sessions.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

STATE_PATH = Path.home() / ".local" / "share" / "claude-orchestrator" / "state.json"


@dataclass
class PersistedState:
    claude_enabled: bool = True
    # Opt-in (SPEC.md §7.4): voice replies are extra synthesis work per
    # message, so unlike claude_enabled this defaults to off.
    voice_enabled: bool = False
    # None means no /project switch has been persisted yet — the dispatcher
    # falls back to config.default_project in that case.
    active_project: str | None = None
    # project name -> Claude Code session id, so a restart resumes the same
    # conversation instead of silently starting a fresh one.
    session_ids: dict[str, str] = field(default_factory=dict)


def load_state(path: Path = STATE_PATH) -> PersistedState:
    if not path.exists():
        return PersistedState()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return PersistedState(
            claude_enabled=bool(data.get("claude_enabled", True)),
            voice_enabled=bool(data.get("voice_enabled", False)),
            active_project=data.get("active_project"),
            session_ids=dict(data.get("session_ids") or {}),
        )
    except (json.JSONDecodeError, OSError):
        logger.exception("failed to read state file %s; defaulting to enabled", path)
        return PersistedState()


def save_state(state: PersistedState, path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(state)), encoding="utf-8")
