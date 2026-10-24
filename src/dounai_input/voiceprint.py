"""Local voiceprint feature extraction.

This is a compact, explainable MFCC statistics embedding.  It runs entirely in
process with NumPy, is not a replacement for a dedicated anti-spoofing speaker
verification model, and must not be used as an authentication factor.

Privacy invariants enforced here:

* raw enrollment audio is never written to disk by this module;
* only the averaged embedding is persisted, in the local SQLite database;
* embeddings are never logged, returned by normal APIs, or sent to providers.
"""

from __future__ import annotations

import io
import unicodedata
import wave
from dataclasses import dataclass

import numpy as np

from .store import Store, VoiceprintRecord

SAMPLE_RATE = 16_000
FRAME_LENGTH = 400  # 25 ms
FRAME_HOP = 160  # 10 ms
FFT_SIZE = 512
MEL_BANDS = 40
MFCC_COEFFICIENTS = 20


class VoiceprintQualityError(ValueError):
    """The supplied sample cannot produce a reliable template."""


@dataclass(frozen=True)
class AudioFeatures:
    embedding: np.ndarray
    duration_seconds: float
    speech_ratio: float
    clipping_ratio: float


@dataclass(frozen=True)
class VoiceprintEnrollment:
    sample_count: int
    dimension: int
    created_at: str


def decode_wav(data: bytes) -> tuple[np.ndarray, int]:
    """Decode mono float32 PCM from an in-memory WAV file."""
    try:
        with wave.open(io.BytesIO(data), "rb") as handle:
            channels = handle.getnchannels()
            sample_width = handle.getsampwidth()
            sample_rate = handle.getframerate()
            frames = handle.getnframes()
            raw = handle.readframes(frames)
    except (wave.Error, EOFError) as exc:
        raise VoiceprintQualityError("Sample must be an uncompressed PCM WAV file.") from exc

    if channels < 1 or sample_width not in {1, 2, 3, 4}:
        raise VoiceprintQualityError("Unsupported WAV channel count or sample width.")
    if sample_rate < 8_000 or sample_rate > 192_000:
        raise VoiceprintQualityError("WAV sample rate must be between 8 kHz and 192 kHz.")

    if sample_width == 1:
        samples = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif sample_width == 2:
        samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif sample_width == 3:
        bytes_array = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
        values = (
            bytes_array[:, 0].astype(np.int32)
            | (bytes_array[:, 1].astype(np.int32) << 8)
            | (bytes_array[:, 2].astype(np.int32) << 16)
        )
        values = np.where(values & 0x800000, values - 0x1000000, values)
        samples = values.astype(np.float32) / 8_388_608.0
    else:
        samples = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2_147_483_648.0

    if channels > 1:
        usable = (samples.size // channels) * channels
        samples = samples[:usable].reshape(-1, channels).mean(axis=1)
    if samples.size == 0:
        raise VoiceprintQualityError("WAV file contains no audio samples.")
    if sample_rate != SAMPLE_RATE:
        samples = _resample(samples, sample_rate, SAMPLE_RATE)
    return np.ascontiguousarray(samples, dtype=np.float32), SAMPLE_RATE


def _resample(samples: np.ndarray, original_rate: int, target_rate: int) -> np.ndarray:
    duration = samples.size / original_rate
    target_size = max(1, round(duration * target_rate))
    source_positions = np.linspace(
        0.0, duration, num=samples.size, endpoint=False, dtype=np.float64
    )
    target_positions = np.linspace(0.0, duration, num=target_size, endpoint=False, dtype=np.float64)
    return np.interp(target_positions, source_positions, samples).astype(np.float32)


def _frames(samples: np.ndarray) -> np.ndarray:
    if samples.size < FRAME_LENGTH:
        samples = np.pad(samples, (0, FRAME_LENGTH - samples.size))
    frame_count = 1 + (samples.size - FRAME_LENGTH) // FRAME_HOP
    shape = (frame_count, FRAME_LENGTH)
    strides = (samples.strides[0] * FRAME_HOP, samples.strides[0])
    return np.lib.stride_tricks.as_strided(samples, shape=shape, strides=strides).copy()


def _mel_filterbank() -> np.ndarray:
    def hz_to_mel(value: np.ndarray | float) -> np.ndarray:
        return 2595.0 * np.log10(1.0 + np.asarray(value) / 700.0)

    def mel_to_hz(value: np.ndarray | float) -> np.ndarray:
        return 700.0 * (10.0 ** (np.asarray(value) / 2595.0) - 1.0)

    low_mel = float(hz_to_mel(20.0))
    high_mel = float(hz_to_mel(7_600.0))
    mel_points = np.linspace(low_mel, high_mel, MEL_BANDS + 2)
    hz_points = mel_to_hz(mel_points)
    bins = np.floor((FFT_SIZE + 1) * hz_points / SAMPLE_RATE).astype(int)
    bins = np.clip(bins, 0, FFT_SIZE // 2)
    bank = np.zeros((MEL_BANDS, FFT_SIZE // 2 + 1), dtype=np.float32)
    for index in range(MEL_BANDS):
        left, center, right = bins[index], bins[index + 1], bins[index + 2]
        if center == left:
            center = left + 1
        if right == center:
            right = center + 1
        for bin_index in range(left, min(center, bank.shape[1])):
            bank[index, bin_index] = (bin_index - left) / max(1, center - left)
        for bin_index in range(center, min(right, bank.shape[1])):
            bank[index, bin_index] = (right - bin_index) / max(1, right - center)
    return bank


_MEL_BANK = _mel_filterbank()
_DCT_BASIS = np.zeros((MFCC_COEFFICIENTS, MEL_BANDS), dtype=np.float32)
for _index in range(MFCC_COEFFICIENTS):
    _scale = np.sqrt(2.0 / MEL_BANDS) if _index else np.sqrt(1.0 / MEL_BANDS)
    _DCT_BASIS[_index] = _scale * np.cos(np.pi / MEL_BANDS * (_index + 0.5) * np.arange(MEL_BANDS))


def _mfcc(signal: np.ndarray) -> np.ndarray:
    window = np.hamming(FRAME_LENGTH).astype(np.float32)
    emphasized = np.append(signal[0], signal[1:] - 0.97 * signal[:-1])
    emphasized_frames = _frames(emphasized.astype(np.float32))
    spectrum = np.fft.rfft(emphasized_frames * window, n=FFT_SIZE)
    power = (np.abs(spectrum) ** 2).astype(np.float32)
    mel_energy = power @ _MEL_BANK.T
    log_mel = np.log(np.maximum(mel_energy, 1e-10))
    coefficients = log_mel @ _DCT_BASIS.T
    deltas = np.diff(coefficients, axis=0, prepend=coefficients[:1])
    return np.concatenate([coefficients, deltas], axis=1).astype(np.float32)


def extract_features(data: bytes) -> AudioFeatures:
    samples, _ = decode_wav(data)
    duration = samples.size / SAMPLE_RATE
    peak = float(np.max(np.abs(samples))) if samples.size else 0.0
    clipping_ratio = float(np.mean(np.abs(samples) >= 0.999))
    if peak > 0:
        samples = samples / peak * 0.95
    frame_rms = np.sqrt(np.mean(_frames(samples) ** 2, axis=1) + np.finfo(np.float32).eps)
    threshold = max(0.003, float(np.max(frame_rms)) * 0.06) if frame_rms.size else 0.003
    voiced = frame_rms > threshold
    speech_ratio = float(np.mean(voiced)) if voiced.size else 0.0
    if speech_ratio < 0.15:
        raise VoiceprintQualityError("Sample contains too little speech.")
    if duration < 0.8:
        raise VoiceprintQualityError("Sample is too short; record at least one second.")
    coefficients = _mfcc(samples)
    frame_mask = np.resize(voiced, coefficients.shape[0])
    if int(np.sum(frame_mask)) < 8:
        raise VoiceprintQualityError("Sample has too few voiced frames.")
    selected = coefficients[frame_mask]
    # Both the average spectral envelope (mean) and its dynamics (standard
    # deviation, including delta coefficients) carry speaker information.
    embedding = np.concatenate([selected.mean(axis=0), selected.std(axis=0)]).astype(np.float32)
    norm = float(np.linalg.norm(embedding))
    if not np.isfinite(norm) or norm < 1e-8:
        raise VoiceprintQualityError("Sample did not yield a usable voiceprint.")
    embedding /= norm
    return AudioFeatures(
        embedding=embedding,
        duration_seconds=duration,
        speech_ratio=speech_ratio,
        clipping_ratio=clipping_ratio,
    )


def cosine_similarity(left: np.ndarray, right: np.ndarray) -> float:
    if left.shape != right.shape:
        raise ValueError("Voiceprint dimensions do not match.")
    denominator = float(np.linalg.norm(left) * np.linalg.norm(right))
    if denominator <= 1e-12:
        return 0.0
    return float(np.dot(left, right) / denominator)


def normalize_wake_phrase(value: str) -> str:
    folded = unicodedata.normalize("NFKC", value).casefold()
    return "".join(
        character
        for character in folded
        if not unicodedata.category(character).startswith(("P", "Z", "S"))
    )


class VoiceprintService:
    def __init__(
        self,
        store: Store,
        *,
        default_enabled: bool,
        threshold: float,
        wake_phrase: str,
        policy_version: str,
        wake_phrase_required: bool = True,
    ) -> None:
        self.store = store
        self.default_enabled = default_enabled
        self.threshold = threshold
        self.wake_phrase = wake_phrase
        self.normalized_wake_phrase = normalize_wake_phrase(wake_phrase)
        self.policy_version = policy_version
        self.wake_phrase_required = wake_phrase_required
        self.store.initialize_settings(
            {"voiceprint_enabled": "true" if default_enabled else "false"}
        )

    @property
    def enabled(self) -> bool:
        value = self.store.get_setting(
            "voiceprint_enabled", "true" if self.default_enabled else "false"
        )
        return value == "true"

    def consent_status(self) -> tuple[bool, str | None, str | None]:
        record = self.store.latest_consent()
        if record is None:
            return False, None, None
        effective = record.granted and record.policy_version == self.policy_version
        return effective, record.policy_version, record.recorded_at

    def set_enabled(self, enabled: bool) -> None:
        self.store.set_setting("voiceprint_enabled", "true" if enabled else "false")
        self.store.audit_privacy("voiceprint_enabled" if enabled else "voiceprint_disabled")

    def grant_consent(self, policy_version: str) -> None:
        if policy_version != self.policy_version:
            raise ValueError("The consent policy version is out of date.")
        self.store.record_consent(granted=True, policy_version=policy_version)
        self.set_enabled(True)

    def withdraw_consent(self) -> bool:
        self.store.record_consent(granted=False, policy_version=self.policy_version)
        self.store.delete_voiceprint(reason="consent_withdrawn")
        self.set_enabled(False)
        return True

    def status(self) -> dict[str, object]:
        consent, policy_version, recorded_at = self.consent_status()
        profile = self.store.get_voiceprint()
        return {
            "enabled": self.enabled,
            "consent_granted": consent,
            "consent_policy_version": policy_version,
            "consent_recorded_at": recorded_at,
            "required_policy_version": self.policy_version,
            "enrolled": profile is not None,
            "enrolled_at": profile.created_at if profile else None,
            "template_dimension": profile.dimension if profile else None,
            "threshold": self.threshold,
            "wake_phrase_required": self.wake_phrase_required,
            "local_only": True,
        }

    @property
    def usable(self) -> bool:
        """Whether the switch, consent and an enrolled template all permit use."""
        consent, _, _ = self.consent_status()
        return self.enabled and consent and self.store.get_voiceprint() is not None

    def _can_use(self) -> bool:
        consent, _, _ = self.consent_status()
        return self.enabled and consent

    def enroll(self, samples: list[bytes], *, subject: str = "default") -> VoiceprintEnrollment:
        if not self.enabled:
            raise PermissionError("Voiceprint is disabled. Enable it before enrolling.")
        if not self.consent_status()[0]:
            raise PermissionError("Explicit voiceprint consent is required before enrolling.")
        if len(samples) < 3:
            raise VoiceprintQualityError("At least three voice samples are required.")
        if len(samples) > 8:
            raise VoiceprintQualityError("At most eight voice samples are accepted.")
        features: list[AudioFeatures] = []
        for sample in samples:
            feature = extract_features(sample)
            if not 1.0 <= feature.duration_seconds <= 20.0:
                raise VoiceprintQualityError("Each sample must be between 1 and 20 seconds.")
            features.append(feature)
        embeddings = np.stack([item.embedding for item in features])
        if len(embeddings) > 1:
            similarities = [
                cosine_similarity(embeddings[left], embeddings[right])
                for left in range(len(embeddings))
                for right in range(left + 1, len(embeddings))
            ]
            if float(np.mean(similarities)) < 0.4:
                raise VoiceprintQualityError(
                    "The samples do not sound consistent; record the same speaker again."
                )
        centroid = embeddings.mean(axis=0)
        norm = float(np.linalg.norm(centroid))
        if norm < 1e-8:
            raise VoiceprintQualityError("Could not build a stable voiceprint.")
        centroid = (centroid / norm).astype(np.float32)
        self.store.save_voiceprint(
            subject=subject,
            embedding=centroid.tobytes(),
            dimension=int(centroid.shape[0]),
            samples=len(samples),
        )
        return VoiceprintEnrollment(
            sample_count=len(samples),
            dimension=int(centroid.shape[0]),
            created_at=str(self.store.get_voiceprint().created_at),  # type: ignore[union-attr]
        )

    def detect(self, wav_bytes: bytes, asr_text: str) -> str:
        """Return ``wakeup`` only for an enrolled, consented speaker.

        Any quality/decode problem degrades to ``none``; a biometric failure must
        not prevent the audio event from reaching downstream.
        """
        if not self._can_use():
            return "none"
        profile = self.store.get_voiceprint()
        if profile is None:
            return "none"
        try:
            feature = extract_features(wav_bytes)
        except (VoiceprintQualityError, ValueError):
            return "none"
        reference = np.frombuffer(profile.embedding, dtype=np.float32)
        if reference.shape != feature.embedding.shape:
            return "none"
        if cosine_similarity(reference, feature.embedding) < self.threshold:
            return "none"
        if self.wake_phrase_required:
            transcript = normalize_wake_phrase(asr_text)
            if not transcript or self.normalized_wake_phrase not in transcript:
                return "none"
        return "wakeup"

    def profile_record(self) -> VoiceprintRecord | None:
        return self.store.get_voiceprint()

    def delete_profile(self) -> bool:
        return self.store.delete_voiceprint(reason="user_request")
