"""Catalog and installer for optional local speech-recognition models.

Model weights are never committed to this repository.  The installer downloads
them into the user's private data directory and records the exact Hugging Face
revision plus a SHA-256 of the main weights file for later verification.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
import os
import shutil
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC
from pathlib import Path

MODEL_STATE_FILE = "dounai-model.json"
WEIGHTS_FILES = ("model.bin", "weights.bin")
REQUIRED_FILES = ("config.json",)


class ModelInstallError(RuntimeError):
    """A model cannot be installed, verified or removed."""


@dataclass(frozen=True)
class ModelSpec:
    id: str
    name: str
    repo_id: str
    approximate_size: str
    license: str
    description: str
    languages: str
    recommended: bool = False


MODEL_CATALOG: tuple[ModelSpec, ...] = (
    ModelSpec(
        id="tiny",
        name="Whisper Tiny",
        repo_id="Systran/faster-whisper-tiny",
        approximate_size="约 75 MB",
        license="MIT（OpenAI Whisper 权重）",
        description="最快、最省资源，适合低配设备与离线唤醒词试听。中文准确率较低。",
        languages="多语言",
    ),
    ModelSpec(
        id="base",
        name="Whisper Base",
        repo_id="Systran/faster-whisper-base",
        approximate_size="约 145 MB",
        license="MIT（OpenAI Whisper 权重）",
        description="轻量折中选择，适合纯 CPU 的日常短语音。",
        languages="多语言",
    ),
    ModelSpec(
        id="small",
        name="Whisper Small",
        repo_id="Systran/faster-whisper-small",
        approximate_size="约 466 MB",
        license="MIT（OpenAI Whisper 权重）",
        description="推荐默认档：中文效果与 CPU 速度平衡，适合情感陪伴场景。",
        languages="多语言",
        recommended=True,
    ),
    ModelSpec(
        id="medium",
        name="Whisper Medium",
        repo_id="Systran/faster-whisper-medium",
        approximate_size="约 1.5 GB",
        license="MIT（OpenAI Whisper 权重）",
        description="更高准确率，建议 8 GB 以上显存或较强的 CPU。",
        languages="多语言",
    ),
    ModelSpec(
        id="large-v3-turbo",
        name="Whisper Large v3 Turbo",
        repo_id="deepdml/faster-whisper-large-v3-turbo-ct2",
        approximate_size="约 1.6 GB",
        license="MIT（OpenAI Whisper 权重）",
        description="大模型准确率与较快速度的折中，建议使用 GPU。",
        languages="多语言",
    ),
    ModelSpec(
        id="large-v3",
        name="Whisper Large v3",
        repo_id="Systran/faster-whisper-large-v3",
        approximate_size="约 3.1 GB",
        license="MIT（OpenAI Whisper 权重）",
        description="本地最高准确率档，建议 8 GB 以上显存。",
        languages="多语言",
    ),
)


def get_model(model_id: str) -> ModelSpec:
    for spec in MODEL_CATALOG:
        if spec.id == model_id:
            return spec
    raise ModelInstallError(f"Unknown local model: {model_id!r}")


def model_directory(models_dir: Path, model_id: str) -> Path:
    get_model(model_id)
    return models_dir / f"whisper-{model_id}"


def is_installed(models_dir: Path, model_id: str) -> bool:
    directory = model_directory(models_dir, model_id)
    state = _read_state(directory)
    if state is None:
        return False
    if state.get("model_id") != model_id:
        return False
    if not any((directory / name).is_file() for name in WEIGHTS_FILES):
        return False
    return all((directory / name).is_file() for name in REQUIRED_FILES)


def installed_models(models_dir: Path) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for spec in MODEL_CATALOG:
        if is_installed(models_dir, spec.id):
            directory = model_directory(models_dir, spec.id)
            state = _read_state(directory) or {}
            result.append(
                {
                    "id": spec.id,
                    "name": spec.name,
                    "path": str(directory),
                    "size_bytes": _directory_size(directory),
                    "revision": state.get("revision"),
                    "installed_at": state.get("installed_at"),
                }
            )
    return result


def install_model(
    models_dir: Path,
    model_id: str,
    *,
    revision: str | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, object]:
    """Download a CTranslate2 Whisper model into a private local directory.

    This function is intentionally not imported by the API process.  Installing
    weights is an explicit CLI action, so a normal server start never performs a
    multi-hundred-megabyte download.
    """

    spec = get_model(model_id)
    if (
        importlib.util.find_spec("huggingface_hub") is None
        or importlib.util.find_spec("faster_whisper") is None
    ):
        raise ModelInstallError(
            "Local ASR dependencies are not installed. Run: "
            'pip install -e ".[local-asr]" (or pip install faster-whisper huggingface-hub).'
        )
    if is_installed(models_dir, model_id):
        return {"status": "already-installed", "model_id": model_id}

    from huggingface_hub import HfApi, snapshot_download  # type: ignore[import-not-found]

    models_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = model_directory(models_dir, model_id)
    staging = models_dir / f".{target.name}.installing-{uuid.uuid4().hex}"
    staging.mkdir(parents=True, mode=0o700)
    try:
        commit = revision
        try:
            if commit is None:
                commit = HfApi().model_info(spec.repo_id).sha
            if progress:
                progress(f"Downloading {spec.repo_id} at revision {commit} ...")
            snapshot_download(
                repo_id=spec.repo_id,
                revision=commit,
                local_dir=str(staging),
            )
        except Exception as exc:
            hint = ""
            if "CERTIFICATE" in str(exc).upper() or "SSL" in str(exc).upper():
                hint = (
                    " TLS certificate verification failed; configure your OS trust "
                    "store or point SSL_CERT_FILE at a CA bundle."
                )
            elif "proxy" in str(exc).lower():
                hint = " Check HTTPS_PROXY/NO_PROXY."
            raise ModelInstallError(
                f"Model download failed ({type(exc).__name__}). "
                "Check network access to huggingface.co, the revision, and disk space." + hint
            ) from exc
        cache_dir = staging / ".cache"
        if cache_dir.exists():
            shutil.rmtree(cache_dir, ignore_errors=True)
        weights = next(
            (staging / name for name in WEIGHTS_FILES if (staging / name).is_file()),
            None,
        )
        if weights is None or not all((staging / name).is_file() for name in REQUIRED_FILES):
            raise ModelInstallError("Downloaded model is missing required files.")
        digest = _sha256(weights)
        state = {
            "schema": "dounai-local-model/1",
            "model_id": model_id,
            "repo_id": spec.repo_id,
            "revision": commit,
            "weights_file": weights.name,
            "weights_sha256": digest,
            "installed_at": _now(),
        }
        (staging / MODEL_STATE_FILE).write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if target.exists():
            shutil.rmtree(target)
        staging.replace(target)
        return {
            "status": "installed",
            "model_id": model_id,
            "path": str(target),
            "revision": commit,
            "weights_sha256": digest,
        }
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def verify_model(models_dir: Path, model_id: str) -> dict[str, object]:
    if not is_installed(models_dir, model_id):
        raise ModelInstallError(f"Model is not installed: {model_id}")
    directory = model_directory(models_dir, model_id)
    state = _read_state(directory) or {}
    weights_name = str(state.get("weights_file", "model.bin"))
    weights = directory / weights_name
    if not weights.is_file():
        raise ModelInstallError("Model weights are missing.")
    actual = _sha256(weights)
    expected = state.get("weights_sha256")
    if expected and actual != expected:
        raise ModelInstallError("Model checksum does not match the install record.")
    return {"model_id": model_id, "verified": True, "weights_sha256": actual}


def remove_model(models_dir: Path, model_id: str) -> bool:
    target = model_directory(models_dir, model_id)
    root = models_dir.resolve()
    resolved = target.resolve()
    if resolved == root or root not in resolved.parents:
        raise ModelInstallError("Refusing to remove a path outside the model directory.")
    if not target.exists():
        return False
    # Refuse to remove a directory that does not look like our installation.
    if _read_state(target) is None:
        raise ModelInstallError("Directory is not a Dounai-managed model installation.")
    shutil.rmtree(target)
    return True


def catalog_entries(models_dir: Path) -> list[dict[str, object]]:
    return [
        {
            **asdict(spec),
            "installed": is_installed(models_dir, spec.id),
        }
        for spec in MODEL_CATALOG
    ]


def _read_state(directory: Path) -> dict[str, object] | None:
    state_path = directory / MODEL_STATE_FILE
    if not state_path.is_file():
        return None
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _directory_size(directory: Path) -> int:
    total = 0
    for root, _, files in os.walk(directory):
        for name in files:
            with contextlib.suppress(OSError):
                total += (Path(root) / name).stat().st_size
    return total


def _now() -> str:
    from datetime import datetime

    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
