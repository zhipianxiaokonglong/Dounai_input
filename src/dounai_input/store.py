"""SQLite-backed persistence for events, media metadata, consent and templates.

SQLite keeps the standalone deployment to a single file and gives transactions
for data export/deletion operations.  Every method opens a short-lived connection;
WAL mode keeps reads responsive while an upload is being written.
"""

from __future__ import annotations

import base64
import contextlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from .events import InputEvent
from .settings_helpers import to_utc_iso

_SCHEMA_VERSION = 2


@dataclass(frozen=True)
class StoredEvent:
    event: InputEvent
    session_id: str
    source: str
    media_id: str | None
    created_at: str

    def public_dict(self) -> dict[str, object]:
        return self.event.contract_dict()


@dataclass(frozen=True)
class ConsentRecord:
    granted: bool
    policy_version: str
    recorded_at: str


@dataclass(frozen=True)
class VoiceprintRecord:
    subject: str
    embedding: bytes
    dimension: int
    samples: int
    created_at: str


@dataclass(frozen=True)
class MediaRecord:
    id: str
    path: str
    filename: str
    mime: str
    size: int
    sha256: str
    created_at: str


@dataclass(frozen=True)
class PurgeResult:
    events: int
    media: int
    media_paths: tuple[str, ...]


class Store:
    def __init__(self, database_path: Path, media_dir: Path) -> None:
        self.database_path = database_path
        self.media_dir = media_dir
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self._initialize()

    # ------------------------------------------------------------------ plumbing

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    timestamp TEXT NOT NULL,
                    modality TEXT NOT NULL,
                    content TEXT NOT NULL,
                    asr_text TEXT NOT NULL,
                    asr_status TEXT NOT NULL DEFAULT 'not_applicable',
                    voiceprint_event TEXT NOT NULL,
                    model_capabilities TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    media_id TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_session
                    ON events(session_id, timestamp DESC);
                CREATE INDEX IF NOT EXISTS idx_events_created
                    ON events(created_at);

                CREATE TABLE IF NOT EXISTS media (
                    id TEXT PRIMARY KEY,
                    path TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    mime TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    event_id TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_media_created ON media(created_at);

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS consents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    granted INTEGER NOT NULL,
                    policy_version TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS voiceprint_profiles (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    subject TEXT NOT NULL,
                    embedding BLOB NOT NULL,
                    dimension INTEGER NOT NULL,
                    samples INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS privacy_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    details_json TEXT NOT NULL
                );
                """
            )
            event_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(events)").fetchall()
            }
            if "asr_status" not in event_columns:
                # v1 databases predate the explicit ASR failure marker.  Backfill
                # what can be inferred without guessing: non-empty audio text is
                # completed, empty audio is unavailable, text/image are N/A.
                connection.execute(
                    "ALTER TABLE events ADD COLUMN asr_status TEXT NOT NULL "
                    "DEFAULT 'not_applicable'"
                )
                connection.execute(
                    """
                    UPDATE events SET asr_status = CASE
                        WHEN modality = 'audio' AND asr_text <> '' THEN 'completed'
                        WHEN modality = 'audio' THEN 'unavailable'
                        ELSE 'not_applicable'
                    END
                    """
                )
            connection.execute(
                """
                INSERT INTO schema_meta(key, value) VALUES('schema_version', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (str(_SCHEMA_VERSION),),
            )
        with contextlib.suppress(OSError):
            self.database_path.chmod(0o600)

    # ------------------------------------------------------------------ settings

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        with self._connect() as connection:
            row = connection.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO settings(key, value) VALUES(?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def initialize_settings(self, defaults: dict[str, str]) -> None:
        with self._connect() as connection:
            for key, value in defaults.items():
                connection.execute(
                    "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (key, value)
                )

    # ------------------------------------------------------------------ events

    def save_event(
        self,
        event: InputEvent,
        *,
        session_id: str,
        source: str,
        media_id: str | None = None,
    ) -> None:
        payload = event.contract_dict()
        now = to_utc_iso(datetime.now(UTC))
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO events(
                    event_id, timestamp, modality, content, asr_text,
                    asr_status, voiceprint_event, model_capabilities, session_id,
                    source, media_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(event.event_id),
                    str(payload["timestamp"]),
                    event.modality,
                    event.content,
                    event.metadata.asr_text,
                    event.metadata.asr_status,
                    event.metadata.voiceprint_event,
                    json.dumps(event.metadata.model_capabilities),
                    session_id,
                    source,
                    media_id,
                    now,
                ),
            )
            if media_id is not None:
                connection.execute(
                    "UPDATE media SET event_id = ? WHERE id = ?", (str(event.event_id), media_id)
                )

    def get_event(self, event_id: str) -> StoredEvent | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()
        return self._row_to_event(row) if row else None

    def recent_context(self, session_id: str, limit: int) -> list[str]:
        if limit <= 0:
            return []
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT modality, content, asr_text FROM events
                WHERE session_id = ? AND modality IN ('text', 'audio')
                ORDER BY timestamp DESC LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        snippets: list[str] = []
        for row in reversed(rows):
            text = row["asr_text"] if row["modality"] == "audio" else row["content"]
            if text:
                snippets.append(str(text))
        return snippets

    def count_events(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM events").fetchone()
        return int(row["count"]) if row else 0

    def data_summary(self) -> dict[str, int]:
        with self._connect() as connection:
            events = connection.execute("SELECT COUNT(*) AS c FROM events").fetchone()["c"]
            media = connection.execute("SELECT COUNT(*) AS c FROM media").fetchone()["c"]
            media_bytes = connection.execute(
                "SELECT COALESCE(SUM(size), 0) AS c FROM media"
            ).fetchone()["c"]
            profile = connection.execute(
                "SELECT COUNT(*) AS c FROM voiceprint_profiles"
            ).fetchone()["c"]
        return {
            "events": int(events),
            "media_files": int(media),
            "media_bytes": int(media_bytes),
            "voiceprint_templates": int(profile),
        }

    # ------------------------------------------------------------------ media

    def save_media(self, record: MediaRecord) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO media(id, path, filename, mime, size, sha256, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.id,
                    record.path,
                    record.filename,
                    record.mime,
                    record.size,
                    record.sha256,
                    record.created_at,
                ),
            )

    def get_media_for_event(self, event_id: str) -> MediaRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM media WHERE event_id = ? LIMIT 1", (event_id,)
            ).fetchone()
        if row is None:
            return None
        return MediaRecord(
            id=row["id"],
            path=row["path"],
            filename=row["filename"],
            mime=row["mime"],
            size=int(row["size"]),
            sha256=row["sha256"],
            created_at=row["created_at"],
        )

    def get_media(self, media_id: str) -> MediaRecord | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM media WHERE id = ?", (media_id,)).fetchone()
        if row is None:
            return None
        return MediaRecord(
            id=row["id"],
            path=row["path"],
            filename=row["filename"],
            mime=row["mime"],
            size=int(row["size"]),
            sha256=row["sha256"],
            created_at=row["created_at"],
        )

    def delete_media(self, media_id: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM media WHERE id = ?", (media_id,))

    # ------------------------------------------------------------------ consent

    def latest_consent(self) -> ConsentRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT granted, policy_version, recorded_at FROM consents ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        return ConsentRecord(
            granted=bool(row["granted"]),
            policy_version=row["policy_version"],
            recorded_at=row["recorded_at"],
        )

    def record_consent(self, *, granted: bool, policy_version: str) -> ConsentRecord:
        now = to_utc_iso(datetime.now(UTC))
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO consents(granted, policy_version, recorded_at) VALUES (?, ?, ?)",
                (1 if granted else 0, policy_version, now),
            )
            self._audit(
                connection,
                "consent_granted" if granted else "consent_withdrawn",
                {"policy_version": policy_version},
                now,
            )
        return ConsentRecord(granted=granted, policy_version=policy_version, recorded_at=now)

    def audit_privacy(self, action: str, details: dict[str, Any] | None = None) -> None:
        with self._connect() as connection:
            self._audit(
                connection,
                action,
                details or {},
                to_utc_iso(datetime.now(UTC)),
            )

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        action: str,
        details: dict[str, Any],
        now: str,
    ) -> None:
        connection.execute(
            "INSERT INTO privacy_audit(action, recorded_at, details_json) VALUES (?, ?, ?)",
            (action, now, json.dumps(details, ensure_ascii=False, sort_keys=True)),
        )

    # ------------------------------------------------------------------ voiceprint

    def save_voiceprint(
        self,
        *,
        subject: str,
        embedding: bytes,
        dimension: int,
        samples: int,
    ) -> None:
        now = to_utc_iso(datetime.now(UTC))
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO voiceprint_profiles(id, subject, embedding, dimension, samples, created_at)
                VALUES (1, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    subject = excluded.subject,
                    embedding = excluded.embedding,
                    dimension = excluded.dimension,
                    samples = excluded.samples,
                    created_at = excluded.created_at
                """,
                (subject, embedding, dimension, samples, now),
            )
            self._audit(
                connection,
                "voiceprint_enrolled",
                {"samples": samples, "dimension": dimension},
                now,
            )

    def get_voiceprint(self) -> VoiceprintRecord | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM voiceprint_profiles WHERE id = 1").fetchone()
        if row is None:
            return None
        return VoiceprintRecord(
            subject=row["subject"],
            embedding=row["embedding"],
            dimension=int(row["dimension"]),
            samples=int(row["samples"]),
            created_at=row["created_at"],
        )

    def delete_voiceprint(self, *, reason: str = "user_request") -> bool:
        now = to_utc_iso(datetime.now(UTC))
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM voiceprint_profiles WHERE id = 1")
            deleted = cursor.rowcount > 0
            self._audit(connection, "voiceprint_deleted", {"reason": reason}, now)
        return deleted

    # ------------------------------------------------------------------ export/purge

    def export_snapshot(self) -> dict[str, Any]:
        with self._connect() as connection:
            events = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM events ORDER BY timestamp ASC"
                ).fetchall()
            ]
            media = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM media ORDER BY created_at ASC"
                ).fetchall()
            ]
            consents = [
                dict(row)
                for row in connection.execute("SELECT * FROM consents ORDER BY id ASC").fetchall()
            ]
            audits = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM privacy_audit ORDER BY id ASC"
                ).fetchall()
            ]
            settings = {
                row["key"]: row["value"]
                for row in connection.execute("SELECT key, value FROM settings").fetchall()
            }
            profile_row = connection.execute(
                "SELECT * FROM voiceprint_profiles WHERE id = 1"
            ).fetchone()

        profile: dict[str, Any] | None = None
        if profile_row is not None:
            profile = {
                "subject": profile_row["subject"],
                "dimension": int(profile_row["dimension"]),
                "samples": int(profile_row["samples"]),
                "created_at": profile_row["created_at"],
                "embedding_base64": base64.b64encode(profile_row["embedding"]).decode("ascii"),
                "format": "float32-little-endian",
            }
        return {
            "schema": "dounai-input-export/1",
            "exported_at": to_utc_iso(datetime.now(UTC)),
            "settings": settings,
            "events": events,
            "media": media,
            "consents": consents,
            "privacy_audit": audits,
            "voiceprint": profile,
        }

    def purge_before(self, cutoff: datetime) -> PurgeResult:
        cutoff_iso = to_utc_iso(cutoff)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, path FROM media
                WHERE created_at < ?
                   OR event_id IN (SELECT event_id FROM events WHERE created_at < ?)
                """,
                (cutoff_iso, cutoff_iso),
            ).fetchall()
            media_ids = [str(row["id"]) for row in rows]
            media_paths = tuple(str(row["path"]) for row in rows)
            events = connection.execute(
                "SELECT event_id FROM events WHERE created_at < ?", (cutoff_iso,)
            ).fetchall()
            event_ids = [str(row["event_id"]) for row in events]
            for media_id in media_ids:
                connection.execute("DELETE FROM media WHERE id = ?", (media_id,))
            for event_id in event_ids:
                connection.execute("DELETE FROM events WHERE event_id = ?", (event_id,))
        return PurgeResult(events=len(event_ids), media=len(media_ids), media_paths=media_paths)

    def delete_all_personal_data(self) -> PurgeResult:
        with self._connect() as connection:
            rows = connection.execute("SELECT id, path FROM media").fetchall()
            media_paths = tuple(str(row["path"]) for row in rows)
            events = connection.execute("SELECT COUNT(*) AS c FROM events").fetchone()["c"]
            connection.execute("DELETE FROM events")
            connection.execute("DELETE FROM media")
            connection.execute("DELETE FROM voiceprint_profiles")
            connection.execute("DELETE FROM consents")
            connection.execute("DELETE FROM privacy_audit")
            connection.execute(
                "INSERT INTO settings(key, value) VALUES('voiceprint_enabled', 'false') "
                "ON CONFLICT(key) DO UPDATE SET value = 'false'"
            )
        return PurgeResult(events=int(events), media=len(media_paths), media_paths=media_paths)

    def purge_media_files(self, paths: tuple[str, ...]) -> int:
        """Delete files referenced by an already-removed database row."""
        removed = 0
        root = self.media_dir.resolve()
        for raw_path in paths:
            candidate = (self.media_dir / raw_path).resolve()
            if root not in candidate.parents:
                continue
            try:
                candidate.unlink(missing_ok=True)
                removed += 1
            except OSError:
                continue
        return removed

    def retention_cutoff(self, hours: int) -> datetime:
        return datetime.now(UTC) - timedelta(hours=hours)

    # ------------------------------------------------------------------ mapping

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> StoredEvent:
        event = InputEvent.model_validate(
            {
                "event_id": UUID(row["event_id"]),
                "timestamp": row["timestamp"],
                "modality": row["modality"],
                "content": row["content"],
                "metadata": {
                    "asr_text": row["asr_text"],
                    "asr_status": row["asr_status"],
                    "voiceprint_event": row["voiceprint_event"],
                    "model_capabilities": json.loads(row["model_capabilities"]),
                },
            }
        )
        return StoredEvent(
            event=event,
            session_id=row["session_id"],
            source=row["source"],
            media_id=row["media_id"],
            created_at=row["created_at"],
        )
