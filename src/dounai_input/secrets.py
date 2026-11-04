"""Encryption-at-rest for user-supplied API keys.

Keys are encrypted with AES-256-GCM.  The master key lives in one of two places:

* ``file`` (default): ``data/.secret.key`` with owner-only permissions.  This keeps
  the gateway dependency-free and portable, but protects only against copying the
  SQLite file or data directory by itself;
* ``keyring``: the OS credential store (Windows Credential Manager, macOS Keychain,
  Secret Service on Linux) through the optional ``keyring`` extra.  The master key
  then never touches the data directory.

``DOUNAI_SECRET_BACKEND`` selects ``auto`` (default: keyring when importable and
usable, otherwise file), ``file`` or ``keyring``.  Either way the master key stays on
this machine: it is never sent anywhere, never logged, and never exported.

No plaintext key is ever written to SQLite, logs, exports or API responses.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import importlib
import logging
import os
import secrets
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

logger = logging.getLogger(__name__)

_MASTER_KEY_BYTES = 32
_NONCE_BYTES = 12
_HKDF_INFO = b"dounai-input/api-connection-key/v1"
_KEYRING_SERVICE = "dounai-input"
_BACKENDS = {"auto", "file", "keyring"}
_ENV_BACKEND = "DOUNAI_SECRET_BACKEND"


class SecretStoreError(RuntimeError):
    """Raised when the local secret store cannot be read or decrypted."""


def _keyring() -> Any | None:
    """Return the ``keyring`` module when it is installed and has a real backend."""
    try:
        module = importlib.import_module("keyring")
    except Exception:  # pragma: no cover - depends on the environment
        return None
    try:
        backend = module.get_keyring()
    except Exception:  # pragma: no cover - defensive
        return None
    module_name = type(backend).__module__.lower()
    if "fail" in module_name or "null" in module_name:
        return None
    return module


class SecretStore:
    def __init__(self, data_dir: Path, *, backend: str | None = None) -> None:
        self.data_dir = data_dir
        self.key_path = data_dir / ".secret.key"
        requested = (backend or os.environ.get(_ENV_BACKEND) or "auto").strip().lower()
        if requested not in _BACKENDS:
            raise SecretStoreError(
                f"{_ENV_BACKEND} must be one of {sorted(_BACKENDS)}, got {requested!r}."
            )
        self._requested_backend = requested
        self._keyring_username = (
            "master-key-"
            + hashlib.sha256(os.path.abspath(str(data_dir)).encode("utf-8")).hexdigest()[:16]
        )
        self._key: bytes | None = None
        self._active_backend: str | None = None

    # ------------------------------------------------------------------ keyring

    def _keyring_backend(self) -> Any | None:
        if self._requested_backend == "file":
            return None
        return _keyring()

    def _keyring_read(self, module: Any) -> bytes | None:
        try:
            stored = module.get_password(_KEYRING_SERVICE, self._keyring_username)
        except Exception as exc:
            raise SecretStoreError("Cannot read the master key from the OS keyring.") from exc
        if not stored:
            return None
        try:
            raw = base64.b64decode(stored.encode("ascii"), validate=True)
        except Exception as exc:
            raise SecretStoreError("The master key stored in the OS keyring is corrupted.") from exc
        if len(raw) != _MASTER_KEY_BYTES:
            raise SecretStoreError("The master key stored in the OS keyring is corrupted.")
        return raw

    def _keyring_write(self, module: Any, raw: bytes) -> None:
        try:
            module.set_password(
                _KEYRING_SERVICE,
                self._keyring_username,
                base64.b64encode(raw).decode("ascii"),
            )
        except Exception as exc:
            raise SecretStoreError("Cannot store the master key in the OS keyring.") from exc

    def _keyring_forget(self, module: Any) -> None:
        with contextlib.suppress(Exception):
            module.delete_password(_KEYRING_SERVICE, self._keyring_username)

    def _from_keyring(self, module: Any) -> bytes:
        raw = self._keyring_read(module)
        if raw is not None:
            return raw
        if self.key_path.exists():
            # Upgrade an existing file-based install without losing stored keys.
            legacy = self._read_key_file()
            self._keyring_write(module, legacy)
            with contextlib.suppress(OSError):
                self.key_path.unlink()
            logger.info("migrated the master key from %s to the OS keyring", self.key_path.name)
            return legacy
        raw = secrets.token_bytes(_MASTER_KEY_BYTES)
        self._keyring_write(module, raw)
        return raw

    # ------------------------------------------------------------------ file

    def _read_key_file(self) -> bytes:
        try:
            raw = self.key_path.read_bytes()
        except OSError as exc:
            raise SecretStoreError("Cannot read the local secret key file.") from exc
        if len(raw) != _MASTER_KEY_BYTES:
            raise SecretStoreError("The local secret key file is corrupted.")
        return raw

    def _from_file(self) -> bytes:
        if self.key_path.exists():
            return self._read_key_file()
        # Look in the OS keyring even when the file backend was requested, so that
        # switching DOUNAI_SECRET_BACKEND never silently strands stored API keys.
        module = _keyring()
        if module is not None:
            # Recover a key that was previously moved into the OS keyring.
            with contextlib.suppress(SecretStoreError):
                recovered = self._keyring_read(module)
                if recovered is not None:
                    self._write_key_file(recovered)
                    self._keyring_forget(module)
                    logger.info("restored the master key from the OS keyring")
                    return recovered
        self.key_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        raw = secrets.token_bytes(_MASTER_KEY_BYTES)
        # Exclusive create avoids two processes silently generating different keys.
        try:
            descriptor = os.open(self.key_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return self._read_key_file()
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            with contextlib.suppress(OSError):
                self.key_path.unlink()
            raise
        return raw

    def _write_key_file(self, raw: bytes) -> None:
        self.key_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self.key_path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            with contextlib.suppress(OSError):
                self.key_path.unlink()
            raise

    # ------------------------------------------------------------------ public API

    def _load_or_create_key(self) -> bytes:
        if self._key is not None:
            return self._key
        module = self._keyring_backend()
        if module is not None:
            try:
                raw = self._from_keyring(module)
                self._active_backend = "keyring"
            except SecretStoreError:
                if self._requested_backend == "keyring":
                    raise
                logger.warning("OS keyring is not usable; falling back to %s", self.key_path.name)
                raw = self._from_file()
                self._active_backend = "file"
        else:
            if self._requested_backend == "keyring":
                raise SecretStoreError(
                    "DOUNAI_SECRET_BACKEND=keyring but no usable keyring backend is installed. "
                    "Install the 'keyring' extra or switch to 'file'."
                )
            raw = self._from_file()
            self._active_backend = "file"
        self._key = raw
        return raw

    def _derive_key(self, *, purpose: str) -> bytes:
        master = self._load_or_create_key()
        return HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=b"dounai-input-secret-store",
            info=_HKDF_INFO + purpose.encode("utf-8"),
        ).derive(master)

    def encrypt(self, plaintext: str, *, purpose: str = "api-key") -> tuple[bytes, bytes]:
        if not plaintext:
            raise ValueError("Secret must not be empty.")
        key = self._derive_key(purpose=purpose)
        nonce = secrets.token_bytes(_NONCE_BYTES)
        ciphertext = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), purpose.encode("utf-8"))
        return ciphertext, nonce

    def decrypt(self, ciphertext: bytes, nonce: bytes, *, purpose: str = "api-key") -> str:
        if not ciphertext or len(nonce) != _NONCE_BYTES:
            raise SecretStoreError("Encrypted secret is malformed.")
        key = self._derive_key(purpose=purpose)
        try:
            plaintext = AESGCM(key).decrypt(nonce, ciphertext, purpose.encode("utf-8"))
        except Exception as exc:
            raise SecretStoreError(
                "Cannot decrypt the stored API key. The master key may have changed."
            ) from exc
        return plaintext.decode("utf-8")

    def delete_master_key(self) -> None:
        """Forget every copy of the master key, on disk and in the OS keyring."""
        try:
            self.key_path.unlink(missing_ok=True)
        finally:
            module = _keyring()
            if module is not None:
                self._keyring_forget(module)
            self._key = None
            self._active_backend = None

    @property
    def backend(self) -> str:
        self._load_or_create_key()
        return "aes-gcm-keyring" if self._active_backend == "keyring" else "aes-gcm-local"


def key_fingerprint(api_key: str) -> str:
    """Non-reversible short identifier for UI/debug, never the key itself."""
    digest = hashes.Hash(hashes.SHA256())
    digest.update(api_key.encode("utf-8"))
    raw = digest.finalize()
    return base64.urlsafe_b64encode(raw)[:10].decode("ascii")
