"""Config loading for the claude-orchestrator (SPEC.md §8)."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .projects import scan_project_root, sort_key
from .socket_bus import default_socket_path

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path(
    os.environ.get(
        "CLAUDE_ORCHESTRATOR_CONFIG",
        Path.home() / ".config" / "claude-orchestrator" / "config.yaml",
    )
)


@dataclass(frozen=True)
class ProjectConfig:
    name: str
    path: Path


@dataclass(frozen=True)
class WhatsAppConfig:
    allowed_sender_jid: str


@dataclass(frozen=True)
class ClaudeCodeConfig:
    binary: str = "claude"
    task_timeout_seconds: int = 900


REMOTE_CONTROL_PERMISSION_MODES = (
    "acceptEdits",
    "auto",
    "bypassPermissions",
    "default",
    "dontAsk",
    "plan",
)


@dataclass(frozen=True)
class RemoteControlConfig:
    """Flags for each project's live `claude --remote-control` session
    (SPEC.md §7.7)."""

    permission_mode: str = "bypassPermissions"
    # Claude in Chrome for the session. Off: browser work goes through
    # the Playwright MCP plugin instead of the owner's real Chrome.
    chrome: bool = False


@dataclass(frozen=True)
class WhisperConfig:
    model: str = "medium"
    device: str = "cpu"
    fallback_device: str = "cpu"
    fallback_model: str = "medium"


@dataclass(frozen=True)
class TransportConfig:
    """The Unix socket the orchestrator listens on (socket_bus.py)."""

    socket_path: Path = field(default_factory=default_socket_path)


@dataclass(frozen=True)
class ProgressUpdatesConfig:
    interval_seconds: int = 10


@dataclass(frozen=True)
class TTSConfig:
    # Maps a langdetect language code to a Piper voice id (SPEC.md §7.4).
    # Any detected language without an entry here falls back to "en".
    voices: dict[str, str] = field(
        default_factory=lambda: {"en": "en_US-ljspeech-high", "es": "es_AR-daniela-high"}
    )
    # CPU is the intended default: Piper is fast enough without a GPU, and
    # this avoids VRAM contention with a CUDA faster-whisper model.
    device: str = "cpu"
    models_dir: Path = field(
        default_factory=lambda: Path.home()
        / ".local"
        / "share"
        / "claude-orchestrator"
        / "piper-voices"
    )


@dataclass(frozen=True)
class BridgeConfig:
    """Only the one field the orchestrator itself needs from the `bridge:`
    section — the rest of that section is Node-bridge-only (SPEC.md §8)."""

    outgoing_staging_dir: Path = Path("/tmp/whatsapp-bridge/outgoing")


@dataclass(frozen=True)
class Config:
    whatsapp: WhatsAppConfig
    projects: list[ProjectConfig] = field(default_factory=list)
    claude_code: ClaudeCodeConfig = field(default_factory=ClaudeCodeConfig)
    remote_control: RemoteControlConfig = field(default_factory=RemoteControlConfig)
    whisper: WhisperConfig = field(default_factory=WhisperConfig)
    tts: TTSConfig = field(default_factory=TTSConfig)
    transport: TransportConfig = field(default_factory=TransportConfig)
    progress_updates: ProgressUpdatesConfig = field(default_factory=ProgressUpdatesConfig)
    bridge: BridgeConfig = field(default_factory=BridgeConfig)
    # Parent directory /create is allowed to make new project folders under
    # (SPEC.md §7.2). None means /create is not configured/available.
    project_root: Path | None = None
    # Folders under project_root, as relative paths, that hold projects
    # instead of being one (SPEC.md §7.2). Each is scanned one level deep.
    project_containers: tuple[str, ...] = ()

    @property
    def default_project(self) -> ProjectConfig | None:
        """The first static project, else the first folder found under
        project_root, else None."""
        if self.projects:
            return self.projects[0]
        found = scan_project_root(self.project_root, self.project_containers).projects
        if not found:
            return None
        name = min(found, key=sort_key)
        return ProjectConfig(name=name, path=found[name])

    def project(self, name: str) -> ProjectConfig | None:
        return next((p for p in self.projects if p.name == name), None)


def load_config(path: Path | None = None) -> Config:
    path = path or DEFAULT_CONFIG_PATH
    if not path.exists():
        raise FileNotFoundError(
            f"config file not found at {path}. See deploy/config.example.yaml."
        )

    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    whatsapp_raw = raw.get("whatsapp") or {}
    if not whatsapp_raw.get("allowed_sender_jid"):
        raise ValueError("config.whatsapp.allowed_sender_jid is required")

    projects_raw = raw.get("projects") or []
    projects = [
        ProjectConfig(name=p["name"], path=Path(p["path"]).expanduser())
        for p in projects_raw
    ]
    for project in projects:
        if not project.path.is_dir():
            raise ValueError(
                f"project '{project.name}' path does not exist or is not a directory: "
                f"{project.path}"
            )

    claude_code_raw = raw.get("claude_code") or {}
    remote_control = _parse_remote_control(raw.get("remote_control") or {})
    whisper_raw = raw.get("whisper") or {}
    tts_raw = raw.get("tts") or {}
    transport_raw = raw.get("transport") or {}
    progress_raw = raw.get("progress_updates") or {}
    bridge_raw = raw.get("bridge") or {}
    project_root_raw = raw.get("project_root")
    project_root = Path(project_root_raw).expanduser() if project_root_raw else None
    project_containers = _parse_project_containers(raw.get("project_containers"), project_root)
    if not projects and project_root is None:
        raise ValueError("config must define projects or project_root")

    return Config(
        whatsapp=WhatsAppConfig(allowed_sender_jid=whatsapp_raw["allowed_sender_jid"]),
        projects=projects,
        claude_code=ClaudeCodeConfig(
            binary=claude_code_raw.get("binary", "claude"),
            task_timeout_seconds=int(claude_code_raw.get("task_timeout_seconds", 900)),
        ),
        remote_control=remote_control,
        whisper=WhisperConfig(
            model=whisper_raw.get("model", "medium"),
            device=whisper_raw.get("device", "cpu"),
            fallback_device=whisper_raw.get("fallback_device", "cpu"),
            fallback_model=whisper_raw.get("fallback_model", "medium"),
        ),
        tts=TTSConfig(
            voices=tts_raw.get(
                "voices", {"en": "en_US-ljspeech-high", "es": "es_AR-daniela-high"}
            ),
            device=tts_raw.get("device", "cpu"),
            models_dir=Path(
                tts_raw.get(
                    "models_dir",
                    Path.home() / ".local" / "share" / "claude-orchestrator" / "piper-voices",
                )
            ).expanduser(),
        ),
        transport=TransportConfig(
            socket_path=Path(transport_raw.get("socket_path") or default_socket_path()).expanduser(),
        ),
        progress_updates=ProgressUpdatesConfig(
            interval_seconds=int(progress_raw.get("interval_seconds", 10)),
        ),
        bridge=BridgeConfig(
            outgoing_staging_dir=Path(
                bridge_raw.get("outgoing_staging_dir", "/tmp/whatsapp-bridge/outgoing")
            ).expanduser(),
        ),
        project_root=project_root,
        project_containers=project_containers,
    )


def _parse_project_containers(raw: object, project_root: Path | None) -> tuple[str, ...]:
    """Normalize `project_containers` to relative names like "c/car". An
    entry that is not a plain relative path inside project_root (absolute,
    empty, ".", "..", or a hidden part) is logged and skipped."""
    if not raw:
        return ()
    if project_root is None:
        logger.warning("project_containers is set but project_root is not; ignoring it")
        return ()
    if not isinstance(raw, list):
        logger.warning("project_containers must be a list; ignoring it")
        return ()
    containers: list[str] = []
    for entry in raw:
        text = str(entry).strip()
        parts = text.rstrip("/").split("/")
        if text.startswith("/") or any(not part or part.startswith(".") for part in parts):
            logger.warning("skipping invalid project_containers entry %r", entry)
            continue
        containers.append("/".join(parts))
    return tuple(containers)


def _parse_remote_control(raw: dict) -> RemoteControlConfig:
    permission_mode = str(raw.get("permission_mode", "bypassPermissions"))
    if permission_mode not in REMOTE_CONTROL_PERMISSION_MODES:
        raise ValueError(
            f"config.remote_control.permission_mode must be one of "
            f"{', '.join(REMOTE_CONTROL_PERMISSION_MODES)}; got {permission_mode!r}"
        )
    chrome = raw.get("chrome", False)
    if not isinstance(chrome, bool):
        raise ValueError(f"config.remote_control.chrome must be true or false; got {chrome!r}")
    return RemoteControlConfig(permission_mode=permission_mode, chrome=chrome)
