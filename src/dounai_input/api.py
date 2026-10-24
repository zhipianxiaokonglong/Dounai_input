"""HTTP API for the Dounai multimodal input gateway."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import re
from datetime import UTC
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.background import BackgroundTask

from .config import Settings
from .errors import (
    AuthenticationError,
    DounaiError,
    InputValidationError,
    NotFoundError,
)
from .events import EVENT_JSON_SCHEMA
from .ingest import IngestionResult, IngestService
from .media import MediaService
from .model_catalog import catalog_entries
from .privacy import PrivacyService
from .providers.manager import ProviderManager
from .services import create_services
from .store import Store

logger = logging.getLogger(__name__)

_SESSION_HEADER = "X-Dounai-Session"
_LANGUAGE_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?$")


class TextInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    session_id: str | None = None
    source: str | None = None


class ProviderSwitch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_id: str = Field(min_length=1, max_length=64)


class PrivacySettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    voiceprint_enabled: bool


class ConsentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy_version: str = Field(min_length=1, max_length=32)
    granted: bool = True


def _services(request: Request) -> dict[str, Any]:
    return request.app.state.services


def _auth_dependency(settings: Settings):
    async def require_auth(
        authorization: Annotated[str | None, Header()] = None,
        x_dounai_token: Annotated[str | None, Header()] = None,
    ) -> None:
        expected = settings.api_token
        if not expected:
            return
        provided = ""
        if authorization and authorization.lower().startswith("bearer "):
            provided = authorization[7:].strip()
        elif x_dounai_token:
            provided = x_dounai_token.strip()
        if not provided or not hmac.compare_digest(provided, expected):
            raise AuthenticationError("A valid bearer token is required.")

    return require_auth


def build_router(settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/v1", dependencies=[Depends(_auth_dependency(settings))])

    @router.get("/contract", tags=["contract"])
    async def event_contract() -> dict[str, object]:
        """Return the frozen JSON Schema consumed by downstream modules."""
        return EVENT_JSON_SCHEMA

    @router.get("/providers", tags=["models"])
    async def list_providers(request: Request) -> dict[str, Any]:
        manager: ProviderManager = _services(request)["providers"]
        return {"providers": manager.list_providers(), "status": manager.status()}

    @router.get("/providers/status", tags=["models"])
    async def provider_status(request: Request) -> dict[str, Any]:
        manager: ProviderManager = _services(request)["providers"]
        return manager.status()

    @router.put("/providers/active", tags=["models"])
    async def switch_provider(payload: ProviderSwitch, request: Request) -> dict[str, Any]:
        manager: ProviderManager = _services(request)["providers"]
        info = manager.set_active(payload.provider_id)
        return {"provider": info, "status": manager.status()}

    @router.get("/models", tags=["models"])
    async def list_local_models(request: Request) -> dict[str, Any]:
        services = _services(request)
        settings: Settings = services["settings"]
        return {
            "models": catalog_entries(settings.models_dir),
            "install_hint": (
                "Install optional ASR dependencies, then run: dounai-input models install small"
            ),
            "weights_committed_to_repo": False,
        }

    @router.post("/inputs/text", status_code=201, tags=["inputs"])
    async def ingest_text(payload: TextInput, request: Request) -> JSONResponse:
        ingest: IngestService = _services(request)["ingest"]
        result = await ingest.ingest_text(
            text=payload.text, session_id=payload.session_id, source=payload.source
        )
        return _event_response(result)

    @router.post("/inputs/image", status_code=201, tags=["inputs"])
    async def ingest_image(
        request: Request,
        file: Annotated[UploadFile, File(description="JPEG, PNG, WebP or GIF")],
        session_id: Annotated[str | None, Form()] = None,
        source: Annotated[str | None, Form()] = None,
    ) -> JSONResponse:
        ingest: IngestService = _services(request)["ingest"]
        result = await ingest.ingest_image(upload=file, session_id=session_id, source=source)
        return _event_response(result)

    @router.post("/inputs/audio", status_code=201, tags=["inputs"])
    async def ingest_audio(
        request: Request,
        file: Annotated[UploadFile, File(description="WAV, MP3, M4A, FLAC, OGG or WebM")],
        session_id: Annotated[str | None, Form()] = None,
        source: Annotated[str | None, Form()] = None,
        language: Annotated[str | None, Form()] = None,
    ) -> JSONResponse:
        if language is not None and not _LANGUAGE_RE.fullmatch(language):
            raise InputValidationError("language must be a BCP-47-like code such as zh or en-US.")
        ingest: IngestService = _services(request)["ingest"]
        result = await ingest.ingest_audio(
            upload=file,
            session_id=session_id,
            source=source,
            language=language,
        )
        return _event_response(result)

    @router.get("/events/{event_id}", tags=["inputs"])
    async def get_event(event_id: str, request: Request) -> dict[str, object]:
        store: Store = _services(request)["store"]
        stored = store.get_event(event_id)
        if stored is None:
            raise NotFoundError("Event not found.")
        return stored.event.contract_dict()

    @router.get("/privacy/policy", tags=["privacy"])
    async def privacy_policy(request: Request) -> dict[str, Any]:
        privacy: PrivacyService = _services(request)["privacy"]
        return {
            "version": privacy.settings.consent_policy_version,
            "sha256": privacy.policy_sha256,
            "text": privacy.policy_text,
        }

    @router.get("/privacy/status", tags=["privacy"])
    async def privacy_status(request: Request) -> dict[str, Any]:
        privacy: PrivacyService = _services(request)["privacy"]
        return privacy.status()

    @router.put("/privacy/settings", tags=["privacy"])
    async def update_privacy_settings(
        payload: PrivacySettingsUpdate, request: Request
    ) -> dict[str, Any]:
        privacy: PrivacyService = _services(request)["privacy"]
        return privacy.set_voiceprint_enabled(payload.voiceprint_enabled)

    @router.post("/privacy/consent", tags=["privacy"])
    async def update_consent(payload: ConsentUpdate, request: Request) -> dict[str, Any]:
        privacy: PrivacyService = _services(request)["privacy"]
        if payload.granted:
            return privacy.grant_consent(payload.policy_version)
        return privacy.withdraw_consent()

    @router.delete("/privacy/consent", tags=["privacy"])
    async def withdraw_consent(request: Request) -> dict[str, Any]:
        privacy: PrivacyService = _services(request)["privacy"]
        return privacy.withdraw_consent()

    @router.post("/privacy/voiceprint/enroll", tags=["privacy"])
    async def enroll_voiceprint(
        request: Request,
        files: Annotated[list[UploadFile], File(description="3-8 PCM WAV samples")],
        subject: Annotated[str, Form(max_length=64)] = "default",
    ) -> dict[str, Any]:
        privacy: PrivacyService = _services(request)["privacy"]
        if len(files) < 3 or len(files) > 8:
            raise InputValidationError("Provide between 3 and 8 WAV voice samples.")
        samples: list[bytes] = []
        for upload in files:
            data = await upload.read(privacy.settings.max_audio_bytes + 1)
            await upload.close()
            samples.append(data)
        return await asyncio.to_thread(privacy.enroll, samples, subject)

    @router.delete("/privacy/voiceprint", tags=["privacy"])
    async def delete_voiceprint(request: Request) -> dict[str, Any]:
        privacy: PrivacyService = _services(request)["privacy"]
        return privacy.delete_voiceprint()

    @router.get("/privacy/export", tags=["privacy"])
    async def export_data(request: Request) -> StreamingResponse:
        privacy: PrivacyService = _services(request)["privacy"]
        archive, filename = await asyncio.to_thread(privacy.export_zip)
        return StreamingResponse(
            archive,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
            background=BackgroundTask(archive.close),
        )

    @router.delete("/privacy/data", tags=["privacy"])
    async def delete_all_data(
        request: Request,
        confirm: Annotated[bool, Query(description="Must be true")] = False,
    ) -> dict[str, Any]:
        if not confirm:
            raise InputValidationError(
                "Pass confirm=true after obtaining explicit user confirmation."
            )
        privacy: PrivacyService = _services(request)["privacy"]
        return privacy.delete_all_data()

    return router


def _event_response(result: IngestionResult) -> JSONResponse:
    return JSONResponse(
        status_code=201,
        content=result.event.contract_dict(),
        headers=result.headers(),
    )


def build_app(settings: Settings) -> FastAPI:
    settings.validate_bind()
    services = create_services(settings)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
        retention_task = asyncio.create_task(_retention_loop(app.state.services["privacy"]))
        try:
            yield
        finally:
            retention_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await retention_task
            await app.state.services["providers"].aclose()

    app = FastAPI(
        title="Dounai Input Gateway",
        version="0.1.0",
        description=(
            "隐私优先的多模态输入接入层：文字、图像、语音 → 标准化输入事件。"
            "接口契约见 /v1/contract。"
        ),
        docs_url="/docs",
        redoc_url=None,
        lifespan=lifespan,
    )

    app.state.services = {
        "settings": services.settings,
        "store": services.store,
        "media": services.media,
        "voiceprint": services.voiceprint,
        "providers": services.providers,
        "privacy": services.privacy,
        "ingest": services.ingest,
    }

    app.include_router(build_router(settings))
    _install_middleware(app, settings)
    _install_error_handlers(app)
    _install_surface_routes(app, settings)
    return app


async def _retention_loop(privacy: PrivacyService) -> None:
    await asyncio.sleep(2)
    while True:
        try:
            await asyncio.to_thread(privacy.purge_expired)
            await asyncio.to_thread(
                privacy.cleanup_unreferenced_media,
                privacy.store.retention_cutoff(24),
            )
        except Exception as exc:
            logger.warning("retention cleanup failed: %s", type(exc).__name__)
        await asyncio.sleep(15 * 60)


def _install_middleware(app: FastAPI, settings: Settings) -> None:
    @app.middleware("http")
    async def guard(request: Request, call_next):  # type: ignore[no-untyped-def]
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.url.path.startswith(
            "/v1/"
        ):
            # A browser form can write to localhost without CORS permission.  Reject
            # cross-site browser writes explicitly so an unauthenticated loopback
            # deployment is not vulnerable to CSRF.  Non-browser clients normally
            # send no Origin header and remain supported.
            fetch_site = request.headers.get("sec-fetch-site", "")
            if fetch_site == "cross-site":
                return _error_json(403, "forbidden_origin", "Cross-site writes are not allowed.")
            origin = request.headers.get("origin")
            if origin:
                origin_parts = urlsplit(origin)
                allowed_hosts = {
                    request.headers.get("host", "").lower(),
                    urlsplit(settings.public_base_url).netloc.lower(),
                }
                if origin_parts.scheme not in {"http", "https"} or (
                    origin_parts.netloc.lower() not in allowed_hosts
                ):
                    return _error_json(
                        403, "forbidden_origin", "Origin does not match this gateway."
                    )
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                declared = int(content_length)
            except ValueError:
                declared = -1
            if declared < 0:
                return _error_json(400, "invalid_request", "Invalid Content-Length header.")
            max_body = max(settings.max_image_bytes, settings.max_audio_bytes) * 8 + 2**20
            if declared > max_body:
                return _error_json(413, "payload_too_large", "Request body is too large.")
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        if request.url.path.startswith("/v1/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.middleware("http")
    async def request_id(request: Request, call_next):  # type: ignore[no-untyped-def]
        from uuid import uuid4

        request_id = uuid4().hex
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


def _install_error_handlers(app: FastAPI) -> None:
    from fastapi.exceptions import RequestValidationError

    @app.exception_handler(RequestValidationError)
    async def handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        fields = []
        for error in exc.errors()[:10]:
            location = ".".join(str(item) for item in error.get("loc", ()))
            fields.append({"field": location, "message": str(error.get("msg", "invalid"))})
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "invalid_request",
                    "message": "Request validation failed.",
                    "fields": fields,
                }
            },
        )

    @app.exception_handler(DounaiError)
    async def handle_dounai(request: Request, exc: DounaiError) -> JSONResponse:
        if exc.status_code >= 500:
            logger.error(
                "request %s failed: %s",
                getattr(request.state, "request_id", "-"),
                exc.message,
            )
        return _error_json(exc.status_code, exc.code, exc.message)

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.exception(
            "unhandled error for request %s",
            getattr(request.state, "request_id", "-"),
        )
        return _error_json(500, "internal_error", "An internal error occurred.")


def _error_json(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message}},
    )


def _install_surface_routes(app: FastAPI, settings: Settings) -> None:
    from importlib.resources import files

    def resource_bytes(name: str) -> bytes:
        return files("dounai_input").joinpath(f"web/{name}").read_bytes()

    @app.get("/", include_in_schema=False)
    async def index() -> Response:
        return Response(
            content=resource_bytes("index.html"),
            media_type="text/html; charset=utf-8",
            headers=_page_headers(),
        )

    @app.get("/assets/app.css", include_in_schema=False)
    async def css() -> Response:
        return Response(
            content=resource_bytes("app.css"),
            media_type="text/css; charset=utf-8",
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/assets/app.js", include_in_schema=False)
    async def js() -> Response:
        return Response(
            content=resource_bytes("app.js"),
            media_type="text/javascript; charset=utf-8",
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/assets/favicon.svg", include_in_schema=False)
    async def favicon() -> Response:
        return Response(
            content=resource_bytes("favicon.svg"),
            media_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon_legacy() -> Response:
        return Response(status_code=204)

    @app.get("/healthz", include_in_schema=False)
    async def health() -> dict[str, object]:
        return {
            "status": "ok",
            "version": "0.1.0",
            "active_provider": app.state.services["providers"].active_id,
            "time_utc": _utc_now(),
        }

    @app.get("/v1/media/{media_id}", include_in_schema=False)
    async def media_file(media_id: str) -> FileResponse:
        # The 128-bit opaque id is a capability URL. It is intentionally outside
        # the authenticated router so downstream <img>/<audio> consumers can fetch
        # it; media URLs are only disclosed through authenticated API responses.
        media: MediaService = app.state.services["media"]
        record, path = media.resolve(media_id)
        return FileResponse(
            path,
            media_type=record.mime,
            filename=record.filename,
            content_disposition_type="inline",
            headers={
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )


def _page_headers() -> dict[str, str]:
    return {
        "Content-Security-Policy": (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' blob: data:; media-src 'self' blob:; connect-src 'self'; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        ),
        "Cache-Control": "no-cache",
        "Referrer-Policy": "no-referrer",
    }


def _utc_now() -> str:
    from datetime import datetime

    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
