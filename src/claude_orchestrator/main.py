"""Entrypoint: wires config, Redis bus, Claude Code runner, transcriber,
and dispatcher together, and runs the event loop (SPEC.md §10)."""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
from pathlib import Path

from .claude_code import ClaudeCodeRunner
from .config import load_config
from .dispatcher import Dispatcher
from .redis_client import RedisBus
from .remote_control import RemoteControl
from .transcription import Transcriber
from .tts import Synthesizer

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


async def _main() -> None:
    _setup_logging()
    config = load_config()
    bus = RedisBus(config.redis)
    runner = ClaudeCodeRunner(binary=config.claude_code.binary)
    transcriber = Transcriber(config.whisper)
    synthesizer = Synthesizer(config.tts, config.bridge.outgoing_staging_dir)
    remote_control = RemoteControl(config.remote_control, config.claude_code.binary)
    dispatcher = Dispatcher(config, bus, runner, transcriber, synthesizer, remote_control)

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
