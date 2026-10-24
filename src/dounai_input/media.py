"""Media validation, private storage and safe retrieval."""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from fastapi import UploadFile

from .errors import InputValidationError, NotFoundError, PayloadTooLargeError, UnsupportedMediaError
from .settings_helpers import to_utc_iso
from .store import MediaRecord, Store

_SAFE_SOURCE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_SAFE_SESSION = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


@dataclass(frozen=True)
class DetectedMedia:
    mime: str
    extension: str
    kind: str


_IMAGE_SIGNATURES: tuple[tuple[bytes, str, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "image/png", "png"),
    (b"GIF87a", "image/gif", "gif"),
    (b"GIF89a", "image/gif", "gif"),
)

_AUDIO_SIGNATURES: tuple[tuple[bytes, str, str], ...] = (
    (b"fLaC", "audio/flac", "flac"),
    (b"OggS", "audio/ogg", "ogg"),
    (b"ID3", "audio/mpeg", "mp3"),
)


def detect_media(data: bytes, expected: str) -> DetectedMedia:
    """Identify media by magic bytes, never by the client-supplied MIME alone."""

    if expected == "image":
        if len(data) < 12:
            raise UnsupportedMediaError("Image file is too small to be valid.")
        for signature, mime, extension in _IMAGE_SIGNATURES:
            if data.startswith(signature):
                return DetectedMedia(mime=mime, extension=extension, kind="image")
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return DetectedMedia(mime="image/webp", extension="webp", kind="image")
        raise UnsupportedMediaError("Unsupported image format. Use JPEG, PNG, WebP or GIF.")

    if expected == "audio":
        if len(data) < 12:
            raise UnsupportedMediaError("Audio file is too small to be valid.")
        for signature, mime, extension in _AUDIO_SIGNATURES:
            if data.startswith(signature):
                return DetectedMedia(mime=mime, extension=extension, kind="audio")
        if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
            return DetectedMedia(mime="audio/wav", extension="wav", kind="audio")
        if data[4:8] == b"ftyp":
            brand = data[8:12].lower()
            if brand in {b"m4a ", b"mp42", b"isom", b"iso2", b"mp41"}:
                return DetectedMedia(mime="audio/mp4", extension="m4a", kind="audio")
        if data.startswith(b"\x1aE\xdf\xa3"):
            return DetectedMedia(mime="audio/webm", extension="webm", kind="audio")
        if len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
            return DetectedMedia(mime="audio/mpeg", extension="mp3", kind="audio")
        raise UnsupportedMediaError(
            "Unsupported audio format. Use WAV, MP3, M4A, FLAC, OGG or WebM."
        )

    raise UnsupportedMediaError(f"Unsupported modality: {expected}")


def normalize_source(raw: str | None) -> str:
    source = (raw or "api").strip()
    if not _SAFE_SOURCE.fullmatch(source):
        raise InputValidationError(
            "source must be 1-64 characters of letters, digits, dot, underscore, colon or dash."
        )
    return source


def normalize_session_id(raw: str | None) -> str:
    if raw is None or not raw.strip():
        return f"sess_{uuid4().hex}"
    session = raw.strip()
    if not _SAFE_SESSION.fullmatch(session):
        raise InputValidationError(
            "session_id must be 1-128 characters of letters, digits, dot, underscore, colon or dash."
        )
    return session


def safe_filename(raw: str | None) -> str:
    """Return a display-only filename; it is never used as a storage path."""
    if not raw:
        return "upload"
    name = Path(raw.replace("\\", "/")).name
    name = unicodedata.normalize("NFKC", name)
    name = "".join(character for character in name if character.isprintable())
    return name[:120] or "upload"


class MediaService:
    def __init__(self, store: Store, media_dir: Path, public_base_url: str) -> None:
        self.store = store
        self.media_dir = media_dir
        self.public_base_url = public_base_url.rstrip("/")

    async def store_upload(
        self,
        upload: UploadFile,
        *,
        expected: str,
        max_bytes: int,
    ) -> tuple[MediaRecord, str]:
        data = await upload.read(max_bytes + 1)
        await upload.close()
        if len(data) > max_bytes:
            raise PayloadTooLargeError(f"File exceeds the {max_bytes // (1024 * 1024)} MiB limit.")
        if not data:
            raise InputValidationError("Uploaded file is empty.")
        detected = detect_media(data, expected)
        media_id = uuid4().hex
        relative = f"{media_id}.{detected.extension}"
        target = self.media_dir / relative
        self._atomic_write(target, data)
        record = MediaRecord(
            id=media_id,
            path=relative,
            filename=safe_filename(upload.filename),
            mime=detected.mime,
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            created_at=to_utc_iso(datetime.now(UTC)),
        )
        try:
            self.store.save_media(record)
        except Exception:
            target.unlink(missing_ok=True)
            raise
        return record, self.content_url(media_id)

    def content_url(self, media_id: str) -> str:
        return f"{self.public_base_url}/v1/media/{media_id}"

    def resolve(self, media_id: str) -> tuple[MediaRecord, Path]:
        if not re.fullmatch(r"[0-9a-f]{32}", media_id):
            raise NotFoundError("Media not found.")
        record = self.store.get_media(media_id)
        if record is None:
            raise NotFoundError("Media not found.")
        root = self.media_dir.resolve()
        candidate = (self.media_dir / record.path).resolve()
        if candidate != root and root not in candidate.parents:
            raise NotFoundError("Media not found.")
        if not candidate.is_file():
            raise NotFoundError("Media file is no longer available.")
        return record, candidate

    def delete(self, media_id: str) -> None:
        record = self.store.get_media(media_id)
        if record is None:
            return
        self.store.delete_media(media_id)
        self.store.purge_media_files((record.path,))

    def purge_before(self, cutoff: datetime) -> int:
        result = self.store.purge_before(cutoff)
        return self.store.purge_media_files(result.media_paths)

    def _atomic_write(self, target: Path, data: bytes) -> None:
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".upload-", dir=target.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            with contextlib.suppress(OSError):
                os.chmod(temporary_name, 0o600)
            os.replace(temporary_name, target)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(temporary_name)
            raise
