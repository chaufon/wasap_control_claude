"""Local text-to-speech for voice replies via Piper.

Runs on CPU by default: Piper is fast enough without a GPU, and staying off
the GPU avoids VRAM contention with a CUDA faster-whisper model.

Voice models are downloaded on first use (same behavior as faster-whisper's
own model download) rather than requiring a separate manual setup step.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import wave
from pathlib import Path
from uuid import uuid4

from langdetect import LangDetectException, detect
from piper.download_voices import download_voice
from piper.voice import PiperVoice

from .config import TTSConfig
from .formatting import strip_for_speech

logger = logging.getLogger(__name__)

_DEFAULT_LANG = "en"


class Synthesizer:
    def __init__(self, config: TTSConfig, staging_dir: Path) -> None:
        self._config = config
        self._staging_dir = staging_dir
        self._voices: dict[str, PiperVoice] = {}
        self._config.models_dir.mkdir(parents=True, exist_ok=True)
        self._staging_dir.mkdir(parents=True, exist_ok=True)

    def _detect_lang(self, text: str) -> str:
        try:
            lang = detect(text)
        except LangDetectException:
            return _DEFAULT_LANG
        return lang if lang in self._config.voices else _DEFAULT_LANG

    def _ensure_voice(self, lang: str) -> PiperVoice:
        if lang in self._voices:
            return self._voices[lang]

        voice_id = self._config.voices[lang]
        model_path = self._config.models_dir / f"{voice_id}.onnx"
        config_path = self._config.models_dir / f"{voice_id}.onnx.json"
        if not model_path.exists() or not config_path.exists():
            logger.info("downloading piper voice '%s' (first use)", voice_id)
            download_voice(voice_id, self._config.models_dir)

        voice = PiperVoice.load(
            model_path, config_path, use_cuda=self._config.device == "cuda"
        )
        self._voices[lang] = voice
        return voice

    def synthesize(self, text: str) -> Path:
        """Render `text` to an Opus/OGG voice note (WhatsApp ptt format),
        returning the path to the file in the outgoing staging directory."""
        spoken_text = strip_for_speech(text)
        lang = self._detect_lang(spoken_text)
        voice = self._ensure_voice(lang)

        stem = self._staging_dir / uuid4().hex
        wav_path = stem.with_suffix(".wav")
        with wave.open(str(wav_path), "wb") as wav_file:
            voice.synthesize_wav(spoken_text, wav_file)

        ogg_path = stem.with_suffix(".ogg")
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(wav_path), "-c:a", "libopus", "-b:a", "32k", str(ogg_path)],
            check=True,
            capture_output=True,
        )
        wav_path.unlink(missing_ok=True)
        return ogg_path

    async def synthesize_async(self, text: str) -> Path:
        return await asyncio.to_thread(self.synthesize, text)
