"""Input validation and event construction for all three modalities."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import UploadFile

from .config import Settings
from .errors import InputValidationError
from .events import AsrStatus, InputEvent
from .media import MediaService, normalize_session_id, normalize_source
from .providers.base import ProviderAttempt, TranscriptionOutcome, TranscriptionRequest
from .providers.manager import ProviderManager
from .store import Store
from .voiceprint import VoiceprintService, ensure_wav_bytes

logger = logging.getLogger(__name__)


@dataclass
class IngestionResult:
    event: InputEvent
    session_id: str
    source: str
    media_id: str | None = None
    provider_id: str | None = None
    model: str | None = None
    degraded: bool = False
    voiceprint_checked: bool = False
    attempts: list[ProviderAttempt] = field(default_factory=list)
    elapsed_ms: int = 0

    def headers(self) -> dict[str, str]:
        headers = {
            "X-Dounai-Session": self.session_id,
            "X-Dounai-Degraded": "true" if self.degraded else "false",
            "X-Dounai-ASR-Status": self.event.metadata.asr_status,
            "X-Dounai-Voiceprint-Checked": "true" if self.voiceprint_checked else "false",
        }
        if self.provider_id:
            headers["X-Dounai-Provider"] = _header_safe(self.provider_id)
        if self.model:
            headers["X-Dounai-Model"] = _header_safe(self.model)
        return headers


def _header_safe(value: str) -> str:
    return "".join(character for character in value if 32 <= ord(character) < 127)[:120]


class IngestService:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        media: MediaService,
        providers: ProviderManager,
        voiceprint: VoiceprintService,
    ) -> None:
        self.settings = settings
        self.store = store
        self.media = media
        self.providers = providers
        self.voiceprint = voiceprint

    async def ingest_text(
        self,
        *,
        text: str,
        session_id: str | None = None,
        source: str | None = None,
    ) -> IngestionResult:
        started = time.monotonic()
        content = _validate_text(text, self.settings.max_text_chars)
        session = normalize_session_id(session_id)
        normalized_source = normalize_source(source)
        event = InputEvent.create(
            modality="text",
            content=content,
            capabilities=self.providers.capabilities(),
            asr_status="not_applicable",
        )
        self.store.save_event(event, session_id=session, source=normalized_source)
        return IngestionResult(
            event=event,
            session_id=session,
            source=normalized_source,
            provider_id=self.providers.active_id,
            model=self.providers.effective_model_label(),
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    async def ingest_image(
        self,
        *,
        upload: UploadFile,
        session_id: str | None = None,
        source: str | None = None,
    ) -> IngestionResult:
        started = time.monotonic()
        session = normalize_session_id(session_id)
        normalized_source = normalize_source(source)
        record, content_url = await self.media.store_upload(
            upload,
            expected="image",
            max_bytes=self.settings.max_image_bytes,
        )
        event = InputEvent.create(
            modality="image",
            content=content_url,
            capabilities=self.providers.capabilities(),
            asr_status="not_applicable",
        )
        try:
            self.store.save_event(
                event,
                session_id=session,
                source=normalized_source,
                media_id=record.id,
            )
        except Exception:
            self.media.delete(record.id)
            raise
        return IngestionResult(
            event=event,
            session_id=session,
            source=normalized_source,
            media_id=record.id,
            provider_id=self.providers.active_id,
            model=self.providers.effective_model_label(),
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    async def ingest_audio(
        self,
        *,
        upload: UploadFile,
        session_id: str | None = None,
        source: str | None = None,
        language: str | None = None,
    ) -> IngestionResult:
        started = time.monotonic()
        session = normalize_session_id(session_id)
        normalized_source = normalize_source(source)
        record, content_url = await self.media.store_upload(
            upload,
            expected="audio",
            max_bytes=self.settings.max_audio_bytes,
        )
        context = tuple(self.store.recent_context(session, self.settings.session_context_events))
        outcome = TranscriptionOutcome()
        try:
            request = TranscriptionRequest(
                path=Path(self.settings.media_dir) / record.path,
                mime=record.mime,
                language=(language or "zh")[:16],
                context=context,
                timeout_seconds=self.settings.provider_timeout_seconds,
            )
            outcome = await self.providers.transcribe(request)
        except Exception as exc:  # defensive: ingest must survive provider bugs
            logger.warning("provider orchestration failed: %s", type(exc).__name__)
            outcome.degraded = True

        voiceprint_event = "none"
        voiceprint_checked = False
        if (
            self.voiceprint.enabled
            and self.voiceprint.usable
            and (record.mime or "").startswith("audio/")
        ):
            try:
                audio_bytes = await asyncio.to_thread(
                    (Path(self.settings.media_dir) / record.path).read_bytes
                )
                wav_bytes = await asyncio.to_thread(ensure_wav_bytes, audio_bytes, record.mime)
                if wav_bytes is not None:
                    voiceprint_event = await asyncio.to_thread(
                        self.voiceprint.detect, wav_bytes, outcome.text
                    )
                    voiceprint_checked = True
            except Exception as exc:
                logger.warning("voiceprint check failed: %s", type(exc).__name__)

        asr_status: AsrStatus
        if outcome.text.strip():
            asr_status = "completed"
        elif outcome.provider_id is not None:
            # A provider ran successfully but detected no speech / returned empty.
            asr_status = "empty"
        else:
            # No ASR-capable provider, all failed, or the selected model cannot
            # transcribe.  This is an explicit capability gap, not usable text.
            asr_status = "unavailable"

        event = InputEvent.create(
            modality="audio",
            content=content_url,
            capabilities=self.providers.capabilities(),
            asr_text=outcome.text,
            asr_status=asr_status,
            voiceprint_event=voiceprint_event,  # type: ignore[arg-type]
        )
        try:
            self.store.save_event(
                event,
                session_id=session,
                source=normalized_source,
                media_id=record.id,
            )
        except Exception:
            self.media.delete(record.id)
            raise
        return IngestionResult(
            event=event,
            session_id=session,
            source=normalized_source,
            media_id=record.id,
            provider_id=outcome.provider_id or self.providers.active_id,
            model=outcome.model or self.providers.effective_model_label(),
            degraded=outcome.degraded or asr_status == "unavailable",
            voiceprint_checked=voiceprint_checked,
            attempts=outcome.attempts,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )


def _validate_text(text: str, max_chars: int) -> str:
    if not isinstance(text, str):
        raise InputValidationError("text must be a string.")
    if "\x00" in text or any(
        ord(character) < 32 and character not in {"\n", "\r", "\t"} for character in text
    ):
        raise InputValidationError("text contains unsupported control characters.")
    if not text.strip():
        raise InputValidationError("text must not be empty.")
    if len(text) > max_chars:
        raise InputValidationError(f"text exceeds the {max_chars} character limit.")
    return text
