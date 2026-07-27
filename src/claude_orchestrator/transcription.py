"""Local voice-note transcription via faster-whisper (SPEC.md §4.3, §5.2).

Uses the GPU (`large-v3`, CUDA/CTranslate2) by default, falling back to a
CPU model only if GPU init fails at runtime (e.g. driver issue) — a
resilience fallback, not the intended default path.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from faster_whisper import WhisperModel

from .config import WhisperConfig

logger = logging.getLogger(__name__)


class Transcriber:
    def __init__(self, config: WhisperConfig) -> None:
        self._config = config
        self._model: WhisperModel | None = None

    def _ensure_model(self) -> WhisperModel:
        if self._model is not None:
            return self._model
        try:
            self._model = WhisperModel(
                self._config.model,
                device=self._config.device,
                compute_type="float16" if self._config.device == "cuda" else "int8",
            )
            logger.info(
                "loaded whisper model %s on %s", self._config.model, self._config.device
            )
        except Exception:
            logger.exception(
                "failed to load whisper model %s on %s; falling back to %s on %s",
                self._config.model,
                self._config.device,
                self._config.fallback_model,
                self._config.fallback_device,
            )
            self._model = WhisperModel(
                self._config.fallback_model,
                device=self._config.fallback_device,
                compute_type="int8",
            )
        return self._model

    def transcribe(self, audio_path: Path) -> str:
        model = self._ensure_model()
        segments, _info = model.transcribe(str(audio_path))
        return " ".join(segment.text.strip() for segment in segments).strip()

    async def transcribe_async(self, audio_path: Path) -> str:
        return await asyncio.to_thread(self.transcribe, audio_path)
