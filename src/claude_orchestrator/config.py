"""Config loading for the claude-orchestrator (SPEC.md §8)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

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


@dataclass(frozen=True)
class WhisperConfig:
    model: str = "large-v3"
    device: str = "cuda"
    fallback_device: str = "cpu"
    fallback_model: str = "medium"


@dataclass(frozen=True)
class RedisConfig:
    host: str = "127.0.0.1"
    port: int = 6379
    incoming_channel: str = "whatsapp:incoming"
    outgoing_channel: str = "whatsapp:outgoing"


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
    # this avoids VRAM contention with faster-whisper's large-v3 model on
    # this machine's 6 GB laptop GPU.
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
    whisper: WhisperConfig = field(default_factory=WhisperConfig)
    tts: TTSConfig = field(default_factory=TTSConfig)
    redis: RedisConfig = field(default_factory=RedisConfig)
    progress_updates: ProgressUpdatesConfig = field(default_factory=ProgressUpdatesConfig)
    bridge: BridgeConfig = field(default_factory=BridgeConfig)
    # Parent directory /create is allowed to make new project folders under
    # (SPEC.md §7.2). None means /create is not configured/available.
    project_root: Path | None = None

    @property
    def default_project(self) -> ProjectConfig:
        if not self.projects:
            raise ValueError("config must define at least one project")
        return self.projects[0]

    def project(self, name: str) -> ProjectConfig | None:
        return next((p for p in self.projects if p.name == name), None)


def load_config(path: Path | None = None) -> Config:
    path = path or DEFAULT_CONFIG_PATH
    if not path.exists():
        raise FileNotFoundError(
            f"config file not found at {path}. See config/config.example.yaml."
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
    if not projects:
        raise ValueError("config.projects must define at least one project")
    for project in projects:
        if not project.path.is_dir():
            raise ValueError(
                f"project '{project.name}' path does not exist or is not a directory: "
                f"{project.path}"
            )

    claude_code_raw = raw.get("claude_code") or {}
    whisper_raw = raw.get("whisper") or {}
    tts_raw = raw.get("tts") or {}
    redis_raw = raw.get("redis") or {}
    progress_raw = raw.get("progress_updates") or {}
    bridge_raw = raw.get("bridge") or {}
    project_root_raw = raw.get("project_root")
    project_root = Path(project_root_raw).expanduser() if project_root_raw else None

    return Config(
        whatsapp=WhatsAppConfig(allowed_sender_jid=whatsapp_raw["allowed_sender_jid"]),
        projects=projects,
        claude_code=ClaudeCodeConfig(
            binary=claude_code_raw.get("binary", "claude"),
            task_timeout_seconds=int(claude_code_raw.get("task_timeout_seconds", 900)),
        ),
        whisper=WhisperConfig(
            model=whisper_raw.get("model", "large-v3"),
            device=whisper_raw.get("device", "cuda"),
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
        redis=RedisConfig(
            host=redis_raw.get("host", "127.0.0.1"),
            port=int(redis_raw.get("port", 6379)),
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
    )
