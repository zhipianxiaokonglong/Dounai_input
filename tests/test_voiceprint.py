"""Local voiceprint quality, consent gating and end-to-end wake decisions."""

from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from dounai_input.api import build_app
from dounai_input.errors import InputValidationError, PrivacyDisabledError
from dounai_input.services import create_services
from dounai_input.voiceprint import (
    VoiceprintQualityError,
    cosine_similarity,
    ensure_wav_bytes,
    extract_features,
    normalize_wake_phrase,
)
from tests.conftest import make_settings
from tests.helpers import silence_wav, synthetic_voice


def _to_webm(wav_bytes: bytes) -> bytes:
    """Encode a WAV clip as WebM/Opus, matching the desktop MediaRecorder."""

    av = pytest.importorskip("av")
    import io

    output = io.BytesIO()
    with av.open(io.BytesIO(wav_bytes)) as source, av.open(output, "w", format="webm") as target:
        input_stream = source.streams.audio[0]
        output_stream = target.add_stream("libopus", rate=16_000)
        output_stream.layout = "mono"
        resampler = av.AudioResampler(format="s16", layout="mono", rate=16_000)

        def mux(frame) -> None:
            frame.sample_rate = 16_000
            for packet in output_stream.encode(frame):
                target.mux(packet)

        for frame in source.decode(input_stream):
            for converted in resampler.resample(frame) or ():
                mux(converted)
        for converted in resampler.resample(None) or ():
            mux(converted)
        for packet in output_stream.encode():
            target.mux(packet)
    return output.getvalue()


def test_same_speaker_is_closer_than_different_speaker() -> None:
    first = extract_features(synthetic_voice(f0=180, seed=1))
    same = extract_features(synthetic_voice(f0=180, seed=8, noise=0.01))
    different = extract_features(synthetic_voice(f0=310, seed=4))
    assert cosine_similarity(first.embedding, same.embedding) > 0.82
    assert cosine_similarity(first.embedding, different.embedding) < 0.6
    assert first.embedding.shape == (80,)


def test_wake_phrase_normalization() -> None:
    assert normalize_wake_phrase("你好，豆奶！") == normalize_wake_phrase("你好豆奶")
    assert normalize_wake_phrase("小 豆 奶") == "小豆奶"
    assert normalize_wake_phrase("Hello, DOUNAI") == "hellodounai"


def test_silence_is_rejected_as_a_sample() -> None:
    with pytest.raises(VoiceprintQualityError):
        extract_features(silence_wav())


def test_enrollment_requires_consent(tmp_path) -> None:
    services = create_services(make_settings(tmp_path))
    services.voiceprint.set_enabled(True)
    with pytest.raises(PermissionError):
        services.voiceprint.enroll(
            [synthetic_voice(), synthetic_voice(seed=2), synthetic_voice(seed=3)]
        )


def test_enrollment_and_detection_flow(tmp_path) -> None:
    services = create_services(make_settings(tmp_path))
    services.privacy.grant_consent("1.0")
    samples = [synthetic_voice(seed=1), synthetic_voice(seed=2), synthetic_voice(seed=3)]
    enrollment = services.privacy.enroll(samples)
    assert enrollment["sample_count"] == 3
    assert enrollment["raw_samples_persisted"] is False
    status = services.privacy.status()
    assert status["enabled"] is True
    assert status["consent_granted"] is True
    assert status["enrolled"] is True
    assert status["local_only"] is True
    assert status["data"]["media_files"] == 0

    positive = synthetic_voice(seed=7, noise=0.008)
    assert services.voiceprint.detect(positive, "你好，豆奶") == "wakeup"
    assert services.voiceprint.detect(positive, "今天天气不错") == "none"
    assert services.voiceprint.detect(synthetic_voice(f0=310, seed=7), "你好豆奶") == "none"


def test_disabled_voiceprint_does_not_detect(tmp_path) -> None:
    services = create_services(make_settings(tmp_path))
    services.privacy.grant_consent("1.0")
    services.privacy.enroll(
        [synthetic_voice(seed=1), synthetic_voice(seed=2), synthetic_voice(seed=3)]
    )
    services.privacy.set_voiceprint_enabled(False)
    assert services.voiceprint.detect(synthetic_voice(seed=7), "你好豆奶") == "none"


def test_withdraw_consent_deletes_template(tmp_path) -> None:
    services = create_services(make_settings(tmp_path))
    services.privacy.grant_consent("1.0")
    services.privacy.enroll(
        [synthetic_voice(seed=1), synthetic_voice(seed=2), synthetic_voice(seed=3)]
    )
    assert services.store.get_voiceprint() is not None
    services.privacy.withdraw_consent()
    assert services.store.get_voiceprint() is None
    assert services.privacy.status()["consent_granted"] is False
    assert services.privacy.status()["enabled"] is False


def test_enrollment_requires_enabled_switch(tmp_path) -> None:
    services = create_services(make_settings(tmp_path))
    services.store.set_setting("voiceprint_enabled", "false")
    with pytest.raises(PrivacyDisabledError):
        services.privacy.enroll(
            [synthetic_voice(), synthetic_voice(seed=2), synthetic_voice(seed=3)]
        )


def test_bad_sample_is_rejected_with_actionable_error(tmp_path) -> None:
    services = create_services(make_settings(tmp_path))
    services.privacy.grant_consent("1.0")
    with pytest.raises(InputValidationError):
        services.privacy.enroll([silence_wav(), silence_wav(), silence_wav()])


def test_api_consent_policy_version_mismatch(client) -> None:
    response = client.post(
        "/v1/privacy/consent",
        json={"policy_version": "0.9", "granted": True},
    )
    assert response.status_code == 409


def test_api_enrollment_and_wakeup(tmp_path) -> None:
    settings = replace(
        make_settings(tmp_path),
        wake_phrase_required=False,
        voiceprint_threshold=0.82,
    )
    with TestClient(build_app(settings)) as client:
        consent = client.post(
            "/v1/privacy/consent",
            json={"policy_version": "1.0", "granted": True},
        )
        assert consent.status_code == 200
        assert consent.json()["enabled"] is True

        samples = [synthetic_voice(seed=1), synthetic_voice(seed=2), synthetic_voice(seed=3)]
        response = client.post(
            "/v1/privacy/voiceprint/enroll",
            files=[
                ("files", (f"sample-{index}.wav", sample, "audio/wav"))
                for index, sample in enumerate(samples)
            ],
        )
        assert response.status_code == 200, response.text
        assert response.json()["enrolled"] is True
        assert response.json()["sample_count"] == 3

        audio = client.post(
            "/v1/inputs/audio",
            files={"file": ("wake.wav", synthetic_voice(seed=9), "audio/wav")},
        )
        assert audio.status_code == 201
        assert audio.json()["metadata"]["voiceprint_event"] == "wakeup"
        assert audio.headers["X-Dounai-Voiceprint-Checked"] == "true"

        status = client.get("/v1/privacy/status").json()
        assert status["data"]["media_files"] == 1  # only the audio event, not enroll samples
        assert status["enrolled"] is True


def test_api_webm_wakeup_is_converted_to_wav(tmp_path) -> None:
    """The desktop records WebM; voiceprint must work for that container too."""

    pytest.importorskip("av")
    settings = replace(
        make_settings(tmp_path),
        wake_phrase_required=False,
        voiceprint_threshold=0.82,
    )
    with TestClient(build_app(settings)) as client:
        client.post("/v1/privacy/consent", json={"policy_version": "1.0", "granted": True})
        samples = [synthetic_voice(seed=1), synthetic_voice(seed=2), synthetic_voice(seed=3)]
        enrolled = client.post(
            "/v1/privacy/voiceprint/enroll",
            files=[
                ("files", (f"sample-{index}.wav", sample, "audio/wav"))
                for index, sample in enumerate(samples)
            ],
        )
        assert enrolled.status_code == 200, enrolled.text

        webm = _to_webm(synthetic_voice(seed=9))
        audio = client.post(
            "/v1/inputs/audio",
            files={"file": ("voice.webm", webm, "audio/webm")},
        )
        assert audio.status_code == 201
        assert audio.json()["metadata"]["voiceprint_event"] == "wakeup"
        assert audio.headers["X-Dounai-Voiceprint-Checked"] == "true"


def test_ensure_wav_passthrough_and_conversion() -> None:
    wav = synthetic_voice(seed=4)
    passthrough = ensure_wav_bytes(wav)
    assert passthrough is not None
    assert passthrough[:4] == b"RIFF"

    pytest.importorskip("av")
    converted = ensure_wav_bytes(_to_webm(wav))
    assert converted is not None
    assert converted[:4] == b"RIFF"
    assert converted[8:12] == b"WAVE"


def test_api_enrollment_requires_consent(client) -> None:
    samples = [synthetic_voice(seed=1), synthetic_voice(seed=2), synthetic_voice(seed=3)]
    response = client.post(
        "/v1/privacy/voiceprint/enroll",
        files=[
            ("files", (f"sample-{index}.wav", sample, "audio/wav"))
            for index, sample in enumerate(samples)
        ],
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] in {"consent_required", "privacy_disabled"}


def test_api_rejects_non_wav_voice_samples(client) -> None:
    client.post("/v1/privacy/consent", json={"policy_version": "1.0", "granted": True})
    response = client.post(
        "/v1/privacy/voiceprint/enroll",
        files=[("files", ("bad.wav", b"not a wav file at all", "audio/wav"))] * 3,
    )
    assert response.status_code == 422
