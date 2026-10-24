"""Optional faster-whisper provider.

The core package never imports the heavy ML stack.  This module is imported only
when the provider is constructed, and faster-whisper itself is imported lazily on
the first transcription.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..errors import ProviderError
from ..model_catalog import (
    REQUIRED_FILES,
    WEIGHTS_FILES,
    ModelInstallError,
    model_directory,
)
from .base import (
    BaseProvider,
    ProviderManifest,
    TranscriptionRequest,
    TranscriptionResult,
)

logger = logging.getLogger(__name__)


class LocalWhisperProvider(BaseProvider):
    manifest = ProviderManifest(
        id="local-whisper",
        name="本地 Whisper（faster-whisper）",
        version="1.0.0",
        description=(
            "完全本地运行的免费语音转写模型。模型权重按需安装，音频不出设备。"
            "使用 CTranslate2 加速，CPU 也可运行。"
        ),
        capabilities=("audio",),
        privacy="local",
        requires_api_key=False,
        can_transcribe=True,
        homepage="https://github.com/SYSTRAN/faster-whisper",
    )

    def __init__(self, config: Mapping[str, Any] | None = None, *, models_dir: Path) -> None:
        super().__init__(config)
        self.models_dir = models_dir
        self.model_id = str(self.config.get("model", "small"))
        self.device = str(self.config.get("device", "auto"))
        self.compute_type = str(self.config.get("compute_type", "int8"))
        self.language = self.config.get("language") or "zh"
        self._model: Any | None = None
        self._load_lock = asyncio.Lock()

    def _model_path(self) -> Path | None:
        candidate = Path(self.model_id).expanduser()
        if candidate.is_absolute() or "/" in self.model_id or "\\" in self.model_id:
            return candidate
        try:
            return model_directory(self.models_dir, self.model_id)
        except ModelInstallError:
            return None

    def _looks_like_model(self, path: Path) -> bool:
        if not path.is_dir():
            return False
        if not all((path / name).is_file() for name in REQUIRED_FILES):
            return False
        return any((path / name).is_file() for name in WEIGHTS_FILES)

    def availability(self) -> tuple[bool, str | None]:
        if importlib.util.find_spec("faster_whisper") is None:
            return (
                False,
                'faster-whisper is not installed. Run: pip install -e ".[local-asr]"',
            )
        path = self._model_path()
        if path is None:
            return False, f"Unknown local model id: {self.model_id!r}."
        if not path.is_dir():
            if "/" in self.model_id or "\\" in self.model_id:
                return False, f"Local model path does not exist: {path}"
            return (
                False,
                f"Model {self.model_id!r} is not installed. "
                f"Run: dounai-input models install {self.model_id}",
            )
        if not self._looks_like_model(path):
            return False, "Local model directory is incomplete."
        return True, None

    async def _get_model(self) -> Any:
        available, reason = self.availability()
        if not available:
            raise ProviderError(
                reason or "Local Whisper is unavailable.",
                provider_id=self.manifest.id,
                retryable=False,
            )
        async with self._load_lock:
            if self._model is None:
                from faster_whisper import WhisperModel  # type: ignore[import-not-found]

                path = self._model_path()
                if path is None:
                    raise ProviderError(
                        "Local Whisper model id is invalid.",
                        provider_id=self.manifest.id,
                        retryable=False,
                    )
                try:
                    self._model = await asyncio.to_thread(
                        WhisperModel,
                        str(path),
                        device=self.device,
                        compute_type=self.compute_type,
                    )
                except Exception as exc:
                    logger.warning("local-whisper model load failed: %s", type(exc).__name__)
                    raise ProviderError(
                        "The local Whisper model could not be loaded. Check the model "
                        "installation and compute_type configuration.",
                        provider_id=self.manifest.id,
                        retryable=False,
                    ) from exc
        return self._model

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        model = await self._get_model()
        language = request.language or self.language or None
        prompt = request.safe_context() or None

        def run_inference() -> tuple[str, Any]:
            # faster-whisper returns a lazy segment generator.  Consume it inside
            # the worker thread or the whole inference would run on the event loop.
            segments, info = model.transcribe(
                str(request.path),
                language=language,
                beam_size=5,
                vad_filter=True,
                condition_on_previous_text=False,
                initial_prompt=prompt,
            )
            return "".join(segment.text for segment in segments).strip(), info

        try:
            text, info = await asyncio.to_thread(run_inference)
        except Exception as exc:
            logger.warning("local-whisper transcription failed: %s", type(exc).__name__)
            raise ProviderError(
                "The local Whisper transcription failed.",
                provider_id=self.manifest.id,
                retryable=True,
            ) from exc
        detected_language = getattr(info, "language", None)
        return TranscriptionResult(
            text=text,
            provider_id=self.manifest.id,
            model=f"faster-whisper/{self.model_id}",
            language=detected_language or language,
        )

    async def aclose(self) -> None:
        self._model = None


def provider_factory(config: Mapping[str, Any], *, models_dir: Path) -> LocalWhisperProvider:
    return LocalWhisperProvider(config, models_dir=models_dir)
