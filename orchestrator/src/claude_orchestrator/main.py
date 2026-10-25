"""Entrypoint: wires config, socket bus, live sessions, transcriber,
synthesizer and dispatcher together, and runs the event loop."""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
import shutil
import sys
from pathlib import Path

import yaml

from .config import Config, load_config
from .dispatcher import Dispatcher
from .live_session import LiveSessions
from .socket_bus import SocketBus, SocketInUse
from .transcription import Transcriber
from .tts import Synthesizer

# EX_CONFIG: a configuration problem a restart cannot fix.
EXIT_CONFIG = 78

LOG_DIR = Path.home() / ".local" / "share" / "claude-orchestrator" / "logs"

logger = logging.getLogger(__name__)


def _setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_DIR / "orchestrator.log", maxBytes=10 * 1024 * 1024, backupCount=5
    )
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(stream_handler)


# Errors load_config raises for a missing, unreadable or invalid file.
CONFIG_ERRORS = (OSError, ValueError, KeyError, TypeError, AttributeError, yaml.YAMLError)


def load_config_or_exit(path: Path | None = None) -> Config:
    """Load the config, or log one line and exit 78 when it is missing,
    unreadable or invalid."""
    try:
        return load_config(path)
    except CONFIG_ERRORS as exc:
        logger.error("configuration error: %s", exc)
        raise SystemExit(EXIT_CONFIG) from exc


async def start_bus(path: Path) -> SocketBus:
    """Start the socket bus, or exit 78 when the socket path is taken or
    not usable."""
    bus = SocketBus(path)
    try:
        await bus.start()
    except SocketInUse as exc:
        logger.error("%s", exc)
        raise SystemExit(EXIT_CONFIG) from exc
    except FileExistsError as exc:
        logger.error("cannot create the socket: %s", exc)
        raise SystemExit(EXIT_CONFIG) from exc
    except PermissionError as exc:
        logger.error("no permission to create the socket at %s: %s", path, exc)
        raise SystemExit(EXIT_CONFIG) from exc
    return bus


def _binary_found(binary: str) -> bool:
    if os.path.isabs(binary):
        return os.path.isfile(binary) and os.access(binary, os.X_OK)
    return shutil.which(binary) is not None


def check_runtime(config: Config) -> list[str]:
    """One problem string per missing binary or unsafe setting; empty when
    the orchestrator can run."""
    problems: list[str] = []
    binary = config.claude_code.binary
    if not _binary_found(binary):
        problems.append(f"claude_code.binary {binary!r} is not an executable file or on PATH")
    for tool in ("tmux", "ffmpeg"):
        if shutil.which(tool) is None:
            problems.append(f"{tool} is not on PATH")
    if os.geteuid() == 0 and config.remote_control.permission_mode == "bypassPermissions":
        problems.append("permission_mode bypassPermissions cannot run as root; use auto")
    return problems


async def _main() -> None:
    _setup_logging()
    config = load_config_or_exit()
    problems = check_runtime(config)
    for problem in problems:
        logger.error("%s", problem)
    if problems:
        raise SystemExit(EXIT_CONFIG)
    bus = await start_bus(config.transport.socket_path)
    transcriber = Transcriber(config.whisper)
    synthesizer = Synthesizer(config.tts, config.bridge.outgoing_staging_dir)
    # The hook command each live session runs (hook_relay.py), with this
    # same interpreter, so it finds this package.
    relay_command = [
        sys.executable, "-m", "claude_orchestrator.hook_relay",
        "--socket", str(config.transport.socket_path),
    ]  # fmt: skip
    live = LiveSessions(config.remote_control, config.claude_code.binary, relay_command)
    dispatcher = Dispatcher(config, bus, transcriber, synthesizer, live)

    logger.info(
        "claude-orchestrator started; projects=%s active=%s",
        [p.name for p in config.projects],
        dispatcher.active_project,
    )
    try:
        await dispatcher.run()
    finally:
        await bus.close()


def run() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    run()
