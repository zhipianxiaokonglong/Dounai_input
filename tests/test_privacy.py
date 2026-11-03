"""Privacy lifecycle: consent, export, deletion and retention."""

from __future__ import annotations

import io
import sqlite3
import zipfile
from datetime import UTC, datetime, timedelta

from dounai_input.store import Store
from tests.helpers import synthetic_voice


def _consent_and_enroll(client) -> None:
    consent = client.post("/v1/privacy/consent", json={"policy_version": "1.0", "granted": True})
    assert consent.status_code == 200
    samples = [synthetic_voice(seed=1), synthetic_voice(seed=2), synthetic_voice(seed=3)]
    response = client.post(
        "/v1/privacy/voiceprint/enroll",
        files=[
            ("files", (f"sample-{index}.wav", sample, "audio/wav"))
            for index, sample in enumerate(samples)
        ],
    )
    assert response.status_code == 200, response.text


def test_privacy_defaults_are_off(client) -> None:
    status = client.get("/v1/privacy/status").json()
    assert status["enabled"] is False
    assert status["consent_granted"] is False
    assert status["enrolled"] is False
    assert status["local_only"] is True
    assert status["policy_sha256"]


def test_policy_is_available_and_hashed(client) -> None:
    policy = client.get("/v1/privacy/policy")
    assert policy.status_code == 200
    payload = policy.json()
    assert payload["version"] == "1.0"
    assert "声纹" in payload["text"]
    assert len(payload["sha256"]) == 64


def test_switch_can_be_enabled_without_consent_but_consent_remains_false(client) -> None:
    response = client.put("/v1/privacy/settings", json={"voiceprint_enabled": True})
    assert response.status_code == 200
    assert response.json()["enabled"] is True
    assert response.json()["consent_granted"] is False


def test_export_contains_manifest_events_media_and_voiceprint(app, client) -> None:
    _consent_and_enroll(client)
    image = client.post(
        "/v1/inputs/image",
        files={"file": ("pixel.png", b"\x89PNG\r\n\x1a\n" + b"0" * 64, "image/png")},
    )
    assert image.status_code == 201
    audio = client.post(
        "/v1/inputs/audio",
        files={"file": ("voice.wav", synthetic_voice(), "audio/wav")},
    )
    assert audio.status_code == 201

    response = client.get("/v1/privacy/export")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "attachment" in response.headers["content-disposition"]
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    names = set(archive.namelist())
    assert {"manifest.json", "data.json", "privacy-policy.md", "media-index.json"} <= names
    assert "voiceprint.json" in names
    data = archive.read("data.json").decode("utf-8")
    assert audio.json()["event_id"] in data
    manifest = archive.read("manifest.json").decode("utf-8")
    assert "biometric" in manifest
    assert any(name.startswith("media/") for name in names)


def test_withdraw_consent_via_api(app, client) -> None:
    _consent_and_enroll(client)
    response = client.delete("/v1/privacy/consent")
    assert response.status_code == 200
    assert response.json()["consent_granted"] is False
    assert response.json()["enrolled"] is False
    assert response.json()["enabled"] is False


def test_delete_voiceprint_keeps_consent(app, client) -> None:
    _consent_and_enroll(client)
    response = client.delete("/v1/privacy/voiceprint")
    assert response.status_code == 200
    assert response.json()["deleted"] is True
    assert response.json()["enrolled"] is False
    assert response.json()["consent_granted"] is True


def test_delete_all_requires_confirmation(client) -> None:
    assert client.delete("/v1/privacy/data").status_code == 422
    assert client.delete("/v1/privacy/data?confirm=false").status_code == 422


def test_delete_all_removes_events_media_voiceprint_and_consent(app, client) -> None:
    _consent_and_enroll(client)
    image = client.post(
        "/v1/inputs/image",
        files={"file": ("pixel.png", b"\x89PNG\r\n\x1a\n" + b"0" * 64, "image/png")},
    ).json()
    media_path = app.state.services["media"].resolve(image["content"].rsplit("/", 1)[-1])[1]
    assert media_path.exists()

    response = client.delete("/v1/privacy/data?confirm=true")
    assert response.status_code == 200
    payload = response.json()
    assert payload["deleted"] is True
    assert payload["events"] == 1
    assert media_path.exists() is False

    status = client.get("/v1/privacy/status").json()
    assert status["data"]["events"] == 0
    assert status["data"]["media_files"] == 0
    assert status["enrolled"] is False
    assert status["consent_granted"] is False
    assert client.get(image["content"]).status_code == 404


def test_retention_purges_expired_events_and_media(app, client) -> None:
    image = client.post(
        "/v1/inputs/image",
        files={"file": ("pixel.png", b"\x89PNG\r\n\x1a\n" + b"0" * 64, "image/png")},
    ).json()
    services = app.state.services
    media = services["media"]
    _record, path = media.resolve(image["content"].rsplit("/", 1)[-1])
    assert path.exists()

    old = (datetime.now(UTC) - timedelta(hours=100)).isoformat()
    with sqlite3.connect(services["settings"].database_path) as connection:
        connection.execute(
            "UPDATE events SET created_at = ? WHERE event_id = ?", (old, image["event_id"])
        )
        # Deliberately leave media.created_at current: retention must follow the
        # event reference, not just independently age media rows.
    result = services["privacy"].purge_expired()
    assert result["events"] == 1
    assert result["files_removed"] == 1
    assert path.exists() is False
    assert services["store"].get_event(image["event_id"]) is None


def test_export_can_be_created_when_empty(client) -> None:
    response = client.get("/v1/privacy/export")
    assert response.status_code == 200
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    assert "data.json" in archive.namelist()
    assert "voiceprint.json" not in archive.namelist()


def test_legacy_v1_database_is_migrated(tmp_path, settings) -> None:
    import sqlite3

    legacy_path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(legacy_path) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE events (
                event_id TEXT PRIMARY KEY,
                timestamp TEXT NOT NULL,
                modality TEXT NOT NULL,
                content TEXT NOT NULL,
                asr_text TEXT NOT NULL,
                voiceprint_event TEXT NOT NULL,
                model_capabilities TEXT NOT NULL,
                session_id TEXT NOT NULL,
                source TEXT NOT NULL,
                media_id TEXT,
                created_at TEXT NOT NULL
            );
            INSERT INTO schema_meta VALUES ('schema_version', '1');
            INSERT INTO events VALUES (
                '00000000-0000-0000-0000-000000000001',
                '2026-01-01T00:00:00Z',
                'audio',
                'http://x/media',
                '你好',
                'none',
                '[]',
                'sess_old',
                'api',
                NULL,
                '2026-01-01T00:00:00Z'
            );
            """
        )
    store = Store(legacy_path, settings.media_dir)
    stored = store.get_event("00000000-0000-0000-0000-000000000001")
    assert stored is not None
    assert stored.event.metadata.asr_status == "completed"
    with sqlite3.connect(legacy_path) as connection:
        version = connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()[0]
    assert version == "2"


def test_store_permissions_are_private_on_posix(settings) -> None:
    import os
    import stat

    store = Store(settings.database_path, settings.media_dir)
    assert store.count_events() == 0
    if os.name != "nt":
        mode = stat.S_IMODE(settings.database_path.stat().st_mode)
        assert mode & 0o077 == 0
