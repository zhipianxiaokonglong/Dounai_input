"""Privacy operations: policy, consent, export, deletion and retention."""

from __future__ import annotations

import hashlib
import json
import logging
import tempfile
import zipfile
from datetime import datetime
from importlib.resources import files
from typing import Any

from .config import Settings
from .errors import ConflictError, ConsentRequiredError, InputValidationError, PrivacyDisabledError
from .media import MediaService
from .store import Store
from .voiceprint import VoiceprintEnrollment, VoiceprintQualityError, VoiceprintService

logger = logging.getLogger(__name__)


class PrivacyService:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        media: MediaService,
        voiceprint: VoiceprintService,
        api_connections: Any | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.media = media
        self.voiceprint = voiceprint
        self.api_connections = api_connections
        self.policy_text = _load_policy_text()
        self.policy_sha256 = hashlib.sha256(self.policy_text.encode("utf-8")).hexdigest()

    def status(self) -> dict[str, Any]:
        status = self.voiceprint.status()
        status.update(
            {
                "policy_version": self.settings.consent_policy_version,
                "policy_sha256": self.policy_sha256,
                "retention_hours": self.settings.media_retention_hours,
                "data": self.store.data_summary(),
            }
        )
        return status

    def grant_consent(self, policy_version: str) -> dict[str, Any]:
        if policy_version != self.settings.consent_policy_version:
            raise ConflictError(
                "The consent policy version does not match the current policy. "
                "Reload the consent text before granting."
            )
        self.voiceprint.grant_consent(policy_version)
        self.store.audit_privacy(
            "consent_granted",
            {
                "policy_version": policy_version,
                "policy_sha256": self.policy_sha256,
            },
        )
        return self.status()

    def withdraw_consent(self) -> dict[str, Any]:
        self.voiceprint.withdraw_consent()
        self.store.audit_privacy("consent_withdrawn", {"policy_sha256": self.policy_sha256})
        return self.status()

    def set_voiceprint_enabled(self, enabled: bool) -> dict[str, Any]:
        self.voiceprint.set_enabled(enabled)
        return self.status()

    def enroll(self, samples: list[bytes], subject: str = "default") -> dict[str, Any]:
        if not self.voiceprint.enabled:
            raise PrivacyDisabledError("Voiceprint is disabled in settings.")
        consent, _, _ = self.voiceprint.consent_status()
        if not consent:
            raise ConsentRequiredError(
                "Explicit consent is required before enrolling a voiceprint."
            )
        cleaned: list[bytes] = []
        for sample in samples:
            if len(sample) > self.settings.max_audio_bytes:
                raise InputValidationError("A voice sample exceeds the audio size limit.")
            if not sample.startswith(b"RIFF") or sample[8:12] != b"WAVE":
                raise InputValidationError("Voiceprint samples must be PCM WAV files.")
            cleaned.append(sample)
        try:
            enrollment: VoiceprintEnrollment = self.voiceprint.enroll(
                cleaned, subject=subject[:64] or "default"
            )
        except VoiceprintQualityError as exc:
            raise InputValidationError(str(exc)) from exc
        except PermissionError as exc:
            raise ConsentRequiredError(str(exc)) from exc
        return {
            "enrolled": True,
            "sample_count": enrollment.sample_count,
            "template_dimension": enrollment.dimension,
            "created_at": enrollment.created_at,
            "local_only": True,
            "raw_samples_persisted": False,
        }

    def delete_voiceprint(self) -> dict[str, Any]:
        deleted = self.voiceprint.delete_profile()
        self.store.audit_privacy("voiceprint_deleted", {"deleted": deleted})
        return {"deleted": deleted, **self.status()}

    def delete_all_data(self) -> dict[str, Any]:
        connections = len(self.store.list_api_connections())
        result = self.store.delete_all_personal_data()
        removed_files = self.store.purge_media_files(result.media_paths)
        if self.api_connections is not None:
            # API keys are personal credentials; drop any dynamic provider
            # instances derived from them and fall back to a local provider.
            self.api_connections.resync()
            if not self.store.list_api_connections():
                # No encrypted keys remain, so the master key itself is now noise.
                self.api_connections.secrets.delete_master_key()
        self.store.audit_privacy(
            "all_personal_data_deleted",
            {"events": result.events, "media": result.media, "files": removed_files},
        )
        return {
            "deleted": True,
            "events": result.events,
            "media_files": result.media,
            "files_removed": removed_files,
            "api_connections_deleted": connections,
            "voiceprint_deleted": True,
            "consent_deleted": True,
        }

    def export_zip(self) -> tuple[tempfile.SpooledTemporaryFile[bytes], str]:
        snapshot = self.store.export_snapshot()
        snapshot["api_connections"] = [
            {
                "id": connection.id,
                "name": connection.name,
                "base_url": connection.base_url,
                "key_fingerprint": connection.key_fingerprint,
                "models": [
                    {
                        "provider_id": model.provider_id,
                        "model_id": model.model_id,
                        "capabilities": list(model.capabilities),
                        "transcription_protocol": model.transcription_protocol,
                    }
                    for model in self.store.list_api_models(connection.id)
                ],
            }
            for connection in self.store.list_api_connections()
        ]
        exported_at = str(snapshot["exported_at"])
        filename = f"dounai-input-export-{exported_at.replace(':', '').replace('-', '')}.zip"
        archive = tempfile.SpooledTemporaryFile(  # noqa: SIM115 - returned to caller
            max_size=16 * 1024 * 1024, mode="w+b"
        )
        with zipfile.ZipFile(archive, mode="w", compression=zipfile.ZIP_DEFLATED) as bundle:
            manifest = {
                "schema": "dounai-input-export/1",
                "contract_version": "1.1",
                "exported_at": exported_at,
                "notice": (
                    "This archive contains personal data including a biometric voiceprint "
                    "template. Store it securely."
                ),
                "policy_version": self.settings.consent_policy_version,
                "policy_sha256": self.policy_sha256,
                "retention_hours": self.settings.media_retention_hours,
            }
            bundle.writestr("manifest.json", _json_bytes(manifest))
            bundle.writestr(
                "data.json",
                _json_bytes({key: value for key, value in snapshot.items() if key != "voiceprint"}),
            )
            if snapshot["voiceprint"] is not None:
                bundle.writestr("voiceprint.json", _json_bytes(snapshot["voiceprint"]))
            bundle.writestr("privacy-policy.md", self.policy_text)
            media_index: list[dict[str, Any]] = []
            for row in snapshot["media"]:
                record = self.store.get_media(str(row["id"]))
                if record is None:
                    continue
                source = (self.settings.media_dir / record.path).resolve()
                root = self.settings.media_dir.resolve()
                if root not in source.parents or not source.is_file():
                    continue
                arcname = f"media/{record.id}.{source.suffix.lstrip('.') or 'bin'}"
                try:
                    bundle.write(source, arcname)
                except OSError:
                    continue
                media_index.append(
                    {
                        "id": record.id,
                        "mime": record.mime,
                        "sha256": record.sha256,
                        "size": record.size,
                        "file": arcname,
                    }
                )
            bundle.writestr("media-index.json", _json_bytes(media_index))
        archive.seek(0)
        return archive, filename

    def purge_expired(self) -> dict[str, int]:
        cutoff = self.store.retention_cutoff(self.settings.media_retention_hours)
        result = self.store.purge_before(cutoff)
        removed = self.store.purge_media_files(result.media_paths)
        if result.events or result.media:
            self.store.audit_privacy(
                "retention_purge",
                {
                    "events": result.events,
                    "media": result.media,
                    "files_removed": removed,
                    "cutoff": cutoff.isoformat(),
                },
            )
            logger.info(
                "retention purge removed %s events and %s media files",
                result.events,
                removed,
            )
        return {"events": result.events, "media": result.media, "files_removed": removed}

    def cleanup_unreferenced_media(self, older_than: datetime) -> int:
        """Remove orphaned files left behind by a crash between write and DB insert."""
        cutoff = older_than.timestamp()
        known = {row["path"] for row in self.store.export_snapshot()["media"]}
        removed = 0
        for path in self.settings.media_dir.iterdir():
            if not path.is_file() or path.name.startswith("."):
                continue
            if path.name in known:
                continue
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
        return removed


def _load_policy_text() -> str:
    resource = files("dounai_input").joinpath("resources/privacy_policy_zh.md")
    return resource.read_text(encoding="utf-8")


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
