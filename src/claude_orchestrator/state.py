"""Persisted on/off toggle for whether messages reach Claude Code.

The self-chat is also used for personal notes/backups (not just Claude Code
commands), so routing is gated by this toggle rather than being always-on.
Persisted across restarts so a deliberate /disable is never silently
undone by a service restart; a fresh install with no state file yet
defaults to enabled, matching the original always-on behavior.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

STATE_PATH = Path.home() / ".local" / "share" / "claude-orchestrator" / "state.json"


@dataclass
class ToggleState:
    claude_enabled: bool = True
    # Opt-in (SPEC.md §7.4): voice replies are extra synthesis work per
    # message, so unlike claude_enabled this defaults to off.
    voice_enabled: bool = False


def load_state(path: Path = STATE_PATH) -> ToggleState:
    if not path.exists():
        return ToggleState()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return ToggleState(
            claude_enabled=bool(data.get("claude_enabled", True)),
            voice_enabled=bool(data.get("voice_enabled", False)),
        )
    except (json.JSONDecodeError, OSError):
        logger.exception("failed to read state file %s; defaulting to enabled", path)
        return ToggleState()


def save_state(state: ToggleState, path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(state)), encoding="utf-8")


DYNAMIC_PROJECTS_PATH = Path.home() / ".local" / "share" / "claude-orchestrator" / "dynamic_projects.json"


@dataclass
class DynamicProjectEntry:
    name: str
    path: str


def load_dynamic_projects(path: Path = DYNAMIC_PROJECTS_PATH) -> list[DynamicProjectEntry]:
    """Projects created via /create (SPEC.md §7.2) — kept separate from
    config.yaml so the hand-curated file is never programmatically rewritten."""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [
            DynamicProjectEntry(name=entry["name"], path=entry["path"])
            for entry in data.get("projects", [])
        ]
    except (json.JSONDecodeError, OSError, KeyError):
        logger.exception("failed to read dynamic projects file %s; ignoring", path)
        return []


def add_dynamic_project(entry: DynamicProjectEntry, path: Path = DYNAMIC_PROJECTS_PATH) -> None:
    entries = load_dynamic_projects(path)
    entries.append(entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"projects": [asdict(e) for e in entries]}
    path.write_text(json.dumps(payload), encoding="utf-8")
