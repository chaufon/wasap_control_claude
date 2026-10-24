"""Entrypoint: wires config, socket bus, live sessions, transcriber,
synthesizer and dispatcher together, and runs the event loop (SPEC.md §10)."""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import sys
from pathlib import Path

from .config import load_config
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


async def start_bus(path: Path) -> SocketBus:
    """Start the socket bus, or exit 78 when the socket path is taken."""
    bus = SocketBus(path)
    try:
        await bus.start()
    except SocketInUse as exc:
        logger.error("%s", exc)
        raise SystemExit(EXIT_CONFIG) from exc
    except FileExistsError as exc:
        logger.error("cannot create the socket: %s", exc)
        raise SystemExit(EXIT_CONFIG) from exc
    return bus


async def _main() -> None:
    _setup_logging()
    config = load_config()
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
        "claude-orchestrator started; projects=%s default=%s",
        [p.name for p in config.projects],
        config.default_project.name,
    )
    try:
        await dispatcher.run()
    finally:
        await bus.close()


def run() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    run()
