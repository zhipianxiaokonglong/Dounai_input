"""Composition root shared by the HTTP app and the CLI."""

from __future__ import annotations

from dataclasses import dataclass

from .config import Settings
from .ingest import IngestService
from .media import MediaService
from .privacy import PrivacyService
from .providers.manager import ProviderManager
from .store import Store
from .voiceprint import VoiceprintService


@dataclass
class Services:
    settings: Settings
    store: Store
    media: MediaService
    voiceprint: VoiceprintService
    providers: ProviderManager
    privacy: PrivacyService
    ingest: IngestService


def create_services(settings: Settings) -> Services:
    settings.ensure_directories()
    store = Store(settings.database_path, settings.media_dir)
    media = MediaService(store, settings.media_dir, settings.public_base_url)
    voiceprint = VoiceprintService(
        store,
        default_enabled=settings.voiceprint_enabled,
        threshold=settings.voiceprint_threshold,
        wake_phrase=settings.wake_phrase,
        policy_version=settings.consent_policy_version,
        wake_phrase_required=settings.wake_phrase_required,
    )
    providers = ProviderManager(settings, store)
    privacy = PrivacyService(settings, store, media, voiceprint)
    ingest = IngestService(settings, store, media, providers, voiceprint)
    return Services(
        settings=settings,
        store=store,
        media=media,
        voiceprint=voiceprint,
        providers=providers,
        privacy=privacy,
        ingest=ingest,
    )
