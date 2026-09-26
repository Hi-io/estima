from __future__ import annotations

import hmac
import asyncio
import json
import logging
import os
from dataclasses import dataclass
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Callable

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope as ASGIScope, Send

from .models import CaseEnvelope, SearchRequest
from .normalize import UnsafeCase, normalize_case, normalize_episode_id, normalize_fingerprint, normalize_instance_id, normalize_scope_filter
from .repository import CredentialUnavailable, EpisodeUnavailable, IdempotencyConflict, InvalidCursor, MAX_RETENTION_DAYS, PostgresEstimaRepository


logger = logging.getLogger("estima")
MAX_BODY_BYTES = 40 * 1024
MIN_TOKEN_BYTES = 24
DEFAULT_CREDENTIAL_OVERLAP_SECONDS = 300
MAX_CREDENTIAL_OVERLAP_SECONDS = 3600
RETENTION_SWEEP_SECONDS = 24 * 60 * 60
RETENTION_RETRY_SECONDS = 5 * 60


@dataclass(frozen=True)
class Principal:
    key_id: str
    role: str
    instance_id: str | None = None


class MaxBodySizeMiddleware:
    def __init__(self, app: ASGIApp, limit: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.limit = limit

    async def __call__(self, scope: ASGIScope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        try:
            declared = int(headers.get(b"content-length", b"0"))
        except ValueError:
            declared = 0
        if declared > self.limit:
            await self._too_large(send)
            return

        chunks = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunks.extend(message.get("body", b""))
            if len(chunks) > self.limit:
                await self._too_large(send)
                return
            if not message.get("more_body", False):
                break

        body = bytes(chunks)
        consumed = False

        async def replay() -> Message:
            nonlocal consumed
            if not consumed:
                consumed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.request", "body": b"", "more_body": False}

        await self.app(scope, replay, send)

    @staticmethod
    async def _too_large(send: Send) -> None:
        body = b'{"detail":"Request body exceeds 40 KiB"}'
        await send({"type": "http.response.start", "status": 413, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": body})


def create_app(
    repository: Any | None = None,
    token: str | None = None,
    *,
    admin_token: str | None = None,
    legacy_instance_id: str | None = None,
    credential_overlap_seconds: int | None = None,
    retention_days: int | None = None,
) -> FastAPI:
    configured_token = token
    configured_admin_token = admin_token
    configured_legacy_instance_id = legacy_instance_id
    configured_overlap_seconds = credential_overlap_seconds
    configured_retention_days = retention_days

    async def retention_worker(app: FastAPI, days: int) -> None:
        delay = RETENTION_SWEEP_SECONDS
        while True:
            await asyncio.sleep(delay)
            try:
                await asyncio.to_thread(app.state.repository.purge_expired_cases, retention_days=days)
            except Exception as exc:
                app.state.retention_ready = False
                logger.error("Collective retention sweep failed (%s)", type(exc).__name__)
                delay = RETENTION_RETRY_SECONDS
            else:
                app.state.retention_ready = True
                delay = RETENTION_SWEEP_SECONDS

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        actual_token = (
            configured_token
            or os.environ.get("COLLECTIVE_API_TOKEN")
            or os.environ.get("ESTIMA_API_TOKEN")
            or os.environ.get("ATLAS_API_TOKEN")
        )
        actual_admin_token = configured_admin_token or os.environ.get("COLLECTIVE_ADMIN_TOKEN")
        instance_id = configured_legacy_instance_id or os.environ.get("COLLECTIVE_LEGACY_INSTANCE_ID")
        if actual_token is None and actual_admin_token is None:
            raise RuntimeError("COLLECTIVE_API_TOKEN or COLLECTIVE_ADMIN_TOKEN must be configured")
        for name, candidate in (("COLLECTIVE_API_TOKEN", actual_token), ("COLLECTIVE_ADMIN_TOKEN", actual_admin_token)):
            if candidate is not None and len(candidate.encode("utf-8")) < MIN_TOKEN_BYTES:
                raise RuntimeError(f"{name} must contain at least 24 bytes")
        if actual_token and actual_admin_token and hmac.compare_digest(
            actual_token.encode("utf-8"), actual_admin_token.encode("utf-8")
        ):
            raise RuntimeError("COLLECTIVE_ADMIN_TOKEN must differ from COLLECTIVE_API_TOKEN")
        if instance_id is not None:
            try:
                instance_id = normalize_instance_id(instance_id)
            except UnsafeCase as exc:
                raise RuntimeError("COLLECTIVE_LEGACY_INSTANCE_ID is invalid") from exc
        overlap_seconds = configured_overlap_seconds
        if overlap_seconds is None:
            raw_overlap = os.environ.get("COLLECTIVE_CREDENTIAL_OVERLAP_SECONDS")
            try:
                overlap_seconds = int(raw_overlap) if raw_overlap is not None else DEFAULT_CREDENTIAL_OVERLAP_SECONDS
            except ValueError:
                raise RuntimeError("COLLECTIVE_CREDENTIAL_OVERLAP_SECONDS must be an integer") from None
        if not 0 <= overlap_seconds <= MAX_CREDENTIAL_OVERLAP_SECONDS:
            raise RuntimeError("COLLECTIVE_CREDENTIAL_OVERLAP_SECONDS must be between 0 and 3600")
        actual_retention_days = configured_retention_days
        if actual_retention_days is None:
            raw_retention_days = os.environ.get("COLLECTIVE_RETENTION_DAYS")
            try:
                actual_retention_days = int(raw_retention_days) if raw_retention_days is not None else None
            except ValueError:
                raise RuntimeError("COLLECTIVE_RETENTION_DAYS must be an integer") from None
        if actual_retention_days is not None and not 1 <= actual_retention_days <= MAX_RETENTION_DAYS:
            raise RuntimeError(f"COLLECTIVE_RETENTION_DAYS must be between 1 and {MAX_RETENTION_DAYS}")
        app.state.token = actual_token
        app.state.admin_token = actual_admin_token
        app.state.legacy_instance_id = instance_id
        app.state.credential_overlap_seconds = overlap_seconds
        app.state.retention_days = actual_retention_days
        app.state.repository = repository
        if app.state.repository is None:
            app.state.repository = PostgresEstimaRepository()
        app.state.repository.migrate()
        app.state.retention_ready = True
        if actual_retention_days is not None:
            await asyncio.to_thread(
                app.state.repository.purge_expired_cases,
                retention_days=actual_retention_days,
            )
        app.state.ready = True
        retention_task = (
            asyncio.create_task(retention_worker(app, actual_retention_days))
            if actual_retention_days is not None
            else None
        )
        try:
            yield
        finally:
            app.state.ready = False
            if retention_task is not None:
                retention_task.cancel()
                try:
                    await retention_task
                except asyncio.CancelledError:
                    pass

    app = FastAPI(title="Collective", version="1.0.0", lifespan=lifespan)
    app.add_middleware(MaxBodySizeMiddleware)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        # Exclude rejected input values so validation failures cannot echo credentials.
        errors = [{"loc": error.get("loc", []), "msg": error.get("msg", "Invalid value"), "type": error.get("type", "value_error")} for error in exc.errors()]
        return JSONResponse(status_code=422, content={"detail": errors})

    def authenticate(request: Request) -> Principal:
        legacy_token = getattr(request.app.state, "token", None)
        admin_token_value = getattr(request.app.state, "admin_token", None)
        if not legacy_token and not admin_token_value:
            raise HTTPException(status_code=503, detail="Collective is not configured")
        authorization = request.headers.get("authorization", "")
        scheme, _, supplied = authorization.partition(" ")
        valid = scheme.casefold() == "bearer" and bool(supplied)
        supplied_bytes = supplied.encode("utf-8") if valid else b""
        if valid and admin_token_value and hmac.compare_digest(
            supplied_bytes, admin_token_value.encode("utf-8")
        ):
            return Principal(key_id="environment-admin", role="admin")
        if valid and legacy_token and hmac.compare_digest(
            supplied_bytes, legacy_token.encode("utf-8")
        ):
            instance_id = getattr(request.app.state, "legacy_instance_id", None)
            return Principal(
                key_id="environment-legacy",
                role="publisher" if instance_id else "reader",
                instance_id=instance_id,
            )
        if not valid:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="A valid bearer token is required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        credential = call_repository(repo(request).authenticate_token, supplied)
        if credential is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="A valid bearer token is required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return Principal(**credential)

    def require_reader(principal: Principal = Depends(authenticate)) -> Principal:
        return principal

    def require_publisher(principal: Principal = Depends(authenticate)) -> Principal:
        if principal.role != "publisher" or not principal.instance_id:
            raise HTTPException(status_code=403, detail="A publisher credential is required")
        return principal

    def require_admin(principal: Principal = Depends(authenticate)) -> Principal:
        if principal.role != "admin":
            raise HTTPException(status_code=403, detail="An admin credential is required")
        return principal

    def repo(request: Request) -> Any:
        instance = getattr(request.app.state, "repository", None)
        if (
            not getattr(request.app.state, "ready", False)
            or not getattr(request.app.state, "retention_ready", True)
            or instance is None
        ):
            raise HTTPException(status_code=503, detail="Collective data service is not ready")
        return instance

    def call_repository(method: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        try:
            return method(*args, **kwargs)
        except IdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except InvalidCursor as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except CredentialUnavailable as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except EpisodeUnavailable as exc:
            raise HTTPException(status_code=410, detail=str(exc)) from None
        except HTTPException:
            raise
        except Exception as exc:
            logger.error("Collective repository request failed (%s)", type(exc).__name__)
            raise HTTPException(status_code=503, detail="Collective data service is unavailable") from None

    @app.get("/healthz", include_in_schema=False)
    def healthz(request: Request) -> JSONResponse:
        instance = getattr(request.app.state, "repository", None)
        if (
            not getattr(request.app.state, "ready", False)
            or not getattr(request.app.state, "retention_ready", True)
            or instance is None
        ):
            return JSONResponse(status_code=503, content={"status": "unavailable"})
        try:
            instance.healthcheck()
        except Exception as exc:
            logger.error("Collective healthcheck failed (%s)", type(exc).__name__)
            return JSONResponse(status_code=503, content={"status": "unavailable"})
        return JSONResponse(content={"status": "ok"})

    @app.post("/v1/cases", status_code=201)
    def create_case(
        envelope: CaseEnvelope,
        request: Request,
        principal: Principal = Depends(require_publisher),
    ) -> JSONResponse:
        try:
            case = normalize_case(envelope)
        except UnsafeCase as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        if case["instance_id"] != principal.instance_id:
            raise HTTPException(status_code=403, detail="Case instance_id does not match the publisher credential")
        result = call_repository(repo(request).create_case, case)
        return JSONResponse(status_code=201 if result["created"] else 200, content=result)

    @app.delete("/v1/episodes/{episode_id}", status_code=204)
    def withdraw_episode(
        request: Request,
        episode_id: str = Path(min_length=1, max_length=128),
        principal: Principal = Depends(require_publisher),
    ) -> Response:
        try:
            normalized_episode_id = normalize_episode_id(episode_id)
        except UnsafeCase as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        call_repository(
            repo(request).withdraw_episode,
            instance_id=principal.instance_id,
            episode_id=normalized_episode_id,
            actor_key_id=principal.key_id,
        )
        return Response(status_code=204)

    @app.get("/v1/stats", dependencies=[Depends(require_reader)])
    def stats(request: Request) -> dict[str, int]:
        return call_repository(repo(request).stats)

    @app.get("/v1/cases", dependencies=[Depends(require_reader)])
    def list_cases(
        request: Request,
        limit: int = Query(default=20, ge=1, le=50),
        cursor: str | None = Query(default=None, min_length=1, max_length=256),
        scope: str | None = Query(default=None, max_length=1000),
        query: str | None = Query(default=None, max_length=500),
        environment: str | None = Query(default=None, max_length=80),
        cluster: str | None = Query(default=None, max_length=200),
        namespace: str | None = Query(default=None, max_length=200),
        service: str | None = Query(default=None, max_length=200),
        workload: str | None = Query(default=None, max_length=200),
        cnfc_id: str | None = Query(default=None, max_length=200),
        vnfc_id: str | None = Query(default=None, max_length=200),
    ) -> dict[str, Any]:
        scope_values: dict[str, Any] = {}
        if scope:
            try:
                parsed_scope = json.loads(scope)
            except json.JSONDecodeError:
                raise HTTPException(status_code=422, detail="scope must be a JSON object") from None
            if not isinstance(parsed_scope, dict):
                raise HTTPException(status_code=422, detail="scope must be a JSON object")
            scope_values.update(parsed_scope)
        for key, value in {
            "environment": environment, "cluster": cluster, "namespace": namespace,
            "service": service, "workload": workload, "cnfc_id": cnfc_id, "vnfc_id": vnfc_id,
        }.items():
            if value is not None:
                scope_values[key] = value
        allowed_scope_keys = {"environment", "cluster", "namespace", "service", "workload", "cnfc_id", "vnfc_id"}
        if set(scope_values) - allowed_scope_keys or any(not isinstance(value, str) for value in scope_values.values()):
            raise HTTPException(status_code=422, detail="scope contains an unknown field or non-string value")
        try:
            scope_filter = normalize_scope_filter(scope_values)
        except UnsafeCase as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        return call_repository(
            repo(request).list_cases,
            scope=scope_filter,
            query=query,
            limit=limit,
            cursor=cursor,
        )

    @app.get("/v1/cases/{case_id}", dependencies=[Depends(require_reader)])
    def get_case(case_id: str, request: Request) -> dict[str, Any]:
        result = call_repository(repo(request).get_case, case_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Case not found")
        return {"case": result}

    @app.post("/v1/search", dependencies=[Depends(require_reader)])
    def search(body: SearchRequest, request: Request) -> dict[str, Any]:
        if body.before and body.observed_before and body.before != body.observed_before:
            raise HTTPException(status_code=422, detail="before and observed_before must match when both are supplied")
        try:
            scope_filter = normalize_scope_filter(body.scope.model_dump(exclude_none=True) if body.scope else None)
            fingerprint = normalize_fingerprint(body.fingerprint)
        except UnsafeCase as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        return call_repository(
            repo(request).search,
            scope=scope_filter,
            query=body.query,
            fingerprint=fingerprint,
            instance_id=body.instance_id,
            observed_after=body.observed_after,
            before=body.observed_before or body.before,
            limit=body.limit,
            cursor=body.cursor,
        )

    @app.get("/v1/patterns", dependencies=[Depends(require_reader)])
    def list_patterns(
        request: Request,
        scope: str | None = Query(default=None),
        query: str | None = Query(default=None, max_length=500),
        limit: int = Query(default=10, ge=1, le=50),
        before: datetime | None = Query(default=None),
        observed_before: datetime | None = Query(default=None),
        environment: str | None = Query(default=None, max_length=80),
        cluster: str | None = Query(default=None, max_length=200),
        namespace: str | None = Query(default=None, max_length=200),
        service: str | None = Query(default=None, max_length=200),
        workload: str | None = Query(default=None, max_length=200),
        cnfc_id: str | None = Query(default=None, max_length=200),
        vnfc_id: str | None = Query(default=None, max_length=200),
    ) -> dict[str, Any]:
        if before and observed_before and before != observed_before:
            raise HTTPException(status_code=422, detail="before and observed_before must match when both are supplied")
        cutoff = observed_before or before
        if cutoff is not None and cutoff.tzinfo is None:
            raise HTTPException(status_code=422, detail="timestamps must include a timezone")
        scope_values: dict[str, Any] = {}
        if scope:
            try:
                parsed_scope = json.loads(scope)
            except json.JSONDecodeError:
                raise HTTPException(status_code=422, detail="scope must be a JSON object") from None
            if not isinstance(parsed_scope, dict):
                raise HTTPException(status_code=422, detail="scope must be a JSON object")
            scope_values.update(parsed_scope)
        for key, value in {
            "environment": environment, "cluster": cluster, "namespace": namespace,
            "service": service, "workload": workload, "cnfc_id": cnfc_id, "vnfc_id": vnfc_id,
        }.items():
            if value is not None:
                scope_values[key] = value
        allowed_scope_keys = {"environment", "cluster", "namespace", "service", "workload", "cnfc_id", "vnfc_id"}
        if set(scope_values) - allowed_scope_keys or any(not isinstance(value, str) for value in scope_values.values()):
            raise HTTPException(status_code=422, detail="scope contains an unknown field or non-string value")
        try:
            scope_filter = normalize_scope_filter(scope_values)
        except UnsafeCase as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        return call_repository(
            repo(request).list_patterns,
            scope=scope_filter,
            query=query,
            before=cutoff,
            limit=limit,
        )

    @app.get("/v1/patterns/{pattern_id}", dependencies=[Depends(require_reader)])
    def get_pattern(pattern_id: str, request: Request) -> dict[str, Any]:
        result = call_repository(repo(request).get_pattern, pattern_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Pattern not found")
        return result

    @app.post("/v1/admin/instances/{instance_id}/publisher-credentials", status_code=201)
    def issue_publisher_credential(
        request: Request,
        instance_id: str = Path(min_length=1, max_length=128),
        principal: Principal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            bound_instance_id = normalize_instance_id(instance_id)
        except UnsafeCase as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        credential = call_repository(
            repo(request).create_credential,
            instance_id=bound_instance_id,
            role="publisher",
            actor_key_id=principal.key_id,
        )
        return credential

    @app.post("/v1/admin/reader-credentials", status_code=201)
    def issue_reader_credential(
        request: Request,
        principal: Principal = Depends(require_admin),
    ) -> dict[str, Any]:
        return call_repository(
            repo(request).create_credential,
            instance_id=None,
            role="reader",
            actor_key_id=principal.key_id,
        )

    @app.post("/v1/credentials/rotate", status_code=201)
    def rotate_credential(
        request: Request,
        principal: Principal = Depends(require_publisher),
    ) -> dict[str, Any]:
        return call_repository(
            repo(request).rotate_credential,
            key_id=principal.key_id,
            actor_key_id=principal.key_id,
            overlap_seconds=request.app.state.credential_overlap_seconds,
        )

    @app.delete("/v1/admin/credentials/{key_id}", status_code=204)
    def revoke_credential(
        key_id: str,
        request: Request,
        principal: Principal = Depends(require_admin),
    ) -> None:
        call_repository(repo(request).revoke_credential, key_id=key_id, actor_key_id=principal.key_id)
        return None

    return app


app = create_app()
