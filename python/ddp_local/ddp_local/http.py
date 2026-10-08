"""Authenticated private loopback HTTP transport over LocalRuntime."""

import asyncio
import hmac
import re
import tempfile
from contextlib import asynccontextmanager, suppress
from urllib.parse import unquote_to_bytes

from ddp_core.application.ports import ApplicationError
from ddp_core.bundle import MAX_ARCHIVE, BundleError

from ddp_local.blobs import MAX_INPUT
from ddp_local.model_runtime.install import settled_io


class _BudgetExceeded(ApplicationError):
    """Internal over-budget signal; the exception handler answers 400 itself."""

    def __init__(self):
        super().__init__("input_too_large", "request body exceeds the byte budget")


try:
    from fastapi import HTTPException as _FastAPIBodyError
except ImportError:  # pragma: no cover — http extra is required to serve
    _FastAPIBodyError = ()


class RequestBodyBudget:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path, method = scope.get("path", ""), scope.get("method", "")
        maximum = (
            65536
            if path in {"/api/v1/search", "/api/v1/answer", "/api/search",
                        "/api/uploads", "/api/wikis"}
            or path.startswith(("/api/v1/wikis", "/api/v1/plans"))
            or (method == "POST" and (
                path.startswith(("/api/documents/", "/api/conversations/"))
                or path.startswith("/api/wikis/") and (
                    path.endswith("/revisions") or path.endswith("/publish"))
                or path.startswith("/api/resources/") and not path.endswith("/parts")
                or path.startswith("/api/v1/models/")))
            or (method in {"PATCH", "PUT"} and (
                path.startswith(("/api/wikis/", "/api/resources/"))))
            else 8 * 1024 * 1024
            if path.startswith("/api/uploads/") and not path.endswith("/finalize") and method == "PUT"
            else MAX_ARCHIVE
        )
        # Reads past the budget are cut off and the over-budget response is
        # substituted for whatever the app was about to send, so an oversize
        # body always reports input_too_large instead of the route's own
        # verdict on a truncated body.
        state = {"total": 0, "hit": False, "sent": False}

        async def bounded_receive():
            message = await receive()
            if message["type"] == "http.request":
                state["total"] += len(message.get("body", b""))
                if state["total"] > maximum:
                    state["hit"] = True
                    return {"type": "http.request", "body": b"", "more_body": False}
            return message

        async def gated_send(message):
            if state["sent"] and message["type"] == "http.response.body":
                return
            if message["type"] != "http.response.start":
                await send(message)
                return
            if state["hit"]:
                state["sent"] = True
                from fastapi.responses import JSONResponse as _BudgetResponse

                rejection = _BudgetResponse(
                    {"error": {"code": "input_too_large",
                               "message": "request body exceeds the byte budget"}}, 400)
                await rejection(scope, receive, send)
                return
            await send(message)

        try:
            await self.app(scope, bounded_receive, gated_send)
        except BaseException as exc:
            if state["hit"] and isinstance(exc, _FastAPIBodyError):
                raise _BudgetExceeded() from exc
            raise


def create_app(
    runtime,
    *,
    session_token: str,
    allowed_hosts: set[str],
    allowed_origins: set[str] | None = None,
    start_worker: bool = True,
    on_shutdown=None,
):
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse
    from pydantic import BaseModel, ConfigDict, Field

    # Request resolves through globals when FastAPI inspects annotations.
    globals()["Request"] = Request
    if (
        len(session_token) < 32
        or not allowed_hosts
        or any(not (h.startswith("127.0.0.1:") or h.startswith("[::1]:")) for h in allowed_hosts)
    ):
        raise ValueError("private API requires a strong token and literal loopback listener hosts")
    origins = allowed_origins or set()

    @asynccontextmanager
    async def lifespan(app):
        # The listener's selected provider may change across a restart. Advance
        # the existing event ledger so resume cannot ACK an old capability view.
        observed = runtime.capabilities()
        with runtime.store.tx():
            runtime.store.event("runtime.started", None, observed)
        worker = asyncio.create_task(runtime.work_forever()) if start_worker else None
        try:
            yield
        finally:
            try:
                if worker:
                    worker.cancel()
                    with suppress(asyncio.CancelledError):
                        await worker
            finally:
                try:
                    await runtime.stop_model()
                finally:
                    if on_shutdown is not None:
                        on_shutdown()

    app = FastAPI(
        title="DDP Local Runtime",
        version="1",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.middleware("http")
    async def boundary(request: Request, call_next):
        # Duplicated sensitive headers are ambiguous across HTTP proxies/parsers.
        for name in ("host", "origin", "authorization"):
            if len(request.headers.getlist(name)) > 1:
                return JSONResponse(
                    {"error": {"code": "invalid_boundary", "message": "ambiguous header"}}, 400
                )
        if request.headers.get("host", "") not in allowed_hosts:
            return JSONResponse(
                {"error": {"code": "invalid_host", "message": "host is not this listener"}}, 403
            )
        origin = request.headers.get("origin")
        if origin is not None and origin not in origins:
            return JSONResponse(
                {"error": {"code": "invalid_origin", "message": "origin is not approved"}}, 403
            )
        expected = "Bearer " + session_token
        if not hmac.compare_digest(
            request.headers.get("authorization", "").encode(), expected.encode()
        ):
            return JSONResponse(
                {"error": {"code": "unauthorized", "message": "session authentication required"}},
                401,
            )
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(ApplicationError)
    @app.exception_handler(BundleError)
    async def application_error(request: Request, exc):
        status = (
            410
            if exc.code == "cursor_expired"
            else 404
            if exc.code in {"not_found", "model_not_found"}
            else 409
            if exc.code in {"idempotency_conflict", "version_not_ready", "source_missing", "model_install_busy", "model_process_busy", "revision_conflict", "task_in_progress", "source_in_use", "wiki_source_unavailable", "wiki_budget_exceeded", "plan_changed", "dispatch_already_reserved"}
            else 503
            if exc.code in {"model_unavailable", "cpu_provider_unavailable", "out_of_memory", "model_not_installed", "runtime_unavailable"}
            else 400
        )
        return JSONResponse({"error": {"code": exc.code, "message": str(exc)}}, status)

    @app.exception_handler(404)
    async def content_not_found(request: Request, exc):
        # Center-shape fallback for /api paths the local subset does not
        # implement. The message comes from the generated source_error
        # labels, never a hand-written copy.
        if isinstance(request.url.path, str) and request.url.path.startswith("/api/"):
            from ddp_contracts.enums import source_error_label

            return JSONResponse(
                {"error": {"code": "not_supported_locally",
                           "message": source_error_label("not_supported_locally")}}, 404,
            )
        from fastapi.responses import JSONResponse as _JSONResponse

        return _JSONResponse({"detail": "Not Found"}, 404)

    @app.exception_handler(405)
    async def content_wrong_method(request: Request, exc):
        # A known path with the wrong method keeps its 405 status (existing
        # private-route tests pin this); only the body becomes the contract
        # shape so Web error parsing sees one error format.
        if isinstance(request.url.path, str) and request.url.path.startswith("/api/"):
            from ddp_contracts.enums import source_error_label

            return JSONResponse(
                {"error": {"code": "not_supported_locally",
                           "message": source_error_label("not_supported_locally")}}, 405,
            )
        from fastapi.responses import JSONResponse as _JSONResponse

        return _JSONResponse({"detail": "Method Not Allowed"}, 405)

    class SearchInput(BaseModel):
        model_config = ConfigDict(extra="forbid")
        query: str = Field(min_length=1, max_length=4096)
        version_ids: list[str] | None = Field(default=None, max_length=1000)
        limit: int = Field(default=10, ge=1, le=100)

    class GenerationInput(BaseModel):
        model_config = ConfigDict(extra="forbid")
        query: str = Field(min_length=1, max_length=4096)
        version_ids: list[str] | None = Field(default=None, max_length=1000)
        execution_policy: str = "local_only"
        allow_remote: bool = False

    class WikiSource(BaseModel):
        model_config = ConfigDict(extra="forbid")
        resource_id: str = Field(min_length=1, max_length=32)
        source_version_id: str = Field(min_length=1, max_length=32)

    class WikiBuild(BaseModel):
        model_config = ConfigDict(extra="forbid")
        title: str = Field(min_length=1, max_length=255)
        sources: list[WikiSource] = Field(min_length=1, max_length=50)
        max_pages: int = Field(default=4, ge=1, le=12)
        max_evidence: int = Field(default=40, ge=1, le=200)
        max_output_tokens: int = Field(default=4096, ge=512, le=8192)
        max_input_chars: int = Field(default=16000, ge=1000, le=50000)
        execution_policy: str = "local_only"
        allow_remote: bool = False

    class WikiRebuild(WikiBuild):
        base_revision_id: str = Field(min_length=1, max_length=32)

    class HumanParagraph(BaseModel):
        model_config = ConfigDict(extra="forbid")
        id: str = Field(min_length=1, max_length=64)
        text: str = Field(min_length=1, max_length=10000)

    class WikiEdit(BaseModel):
        model_config = ConfigDict(extra="forbid")
        base_revision_id: str = Field(min_length=1, max_length=32)
        paragraphs: list[HumanParagraph] = Field(max_length=100)

    async def body_file(request, maximum):
        stream = tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b")
        total = 0
        try:
            async for part in request.stream():
                total += len(part)
                if total > maximum:
                    raise ApplicationError(
                        "input_too_large", "request body exceeds the byte budget"
                    )
                stream.write(part)
            stream.seek(0)
            return stream
        except BaseException:
            stream.close()
            raise

    def operation_key(request):
        keys = request.headers.getlist("idempotency-key")
        if len(keys) != 1:
            raise ApplicationError("invalid_key", "one unambiguous Idempotency-Key is required")
        return keys[0]

    def upload_filename(request):
        raw = request.headers.getlist("x-filename")
        encoded = request.headers.getlist("x-filename-encoded")
        if len(raw) > 1 or len(encoded) > 1 or (raw and encoded):
            raise ApplicationError("invalid_filename", "filename headers must have one unambiguous value")
        if encoded:
            value = encoded[0]
            if not value.isascii() or re.search(r"%(?![0-9a-fA-F]{2})", value):
                raise ApplicationError("invalid_filename", "encoded filename must use valid percent escapes")
            try:
                return runtime.filename(unquote_to_bytes(value).decode("utf-8", errors="strict"))
            except UnicodeError as exc:
                raise ApplicationError("invalid_filename", "encoded filename must contain valid UTF-8") from exc
        return runtime.filename(raw[0] if raw else None)

    @app.get("/api/v1/capabilities")
    async def capabilities():
        return runtime.capabilities()

    @app.get("/api/v1/models")
    async def models():
        return runtime.models()

    @app.post("/api/v1/models/{identifier}/install")
    async def model_install(identifier: str, request: Request):
        # Only the reviewed catalog ID is accepted; no caller-controlled URL,
        # manifest, local path or executable enters the HTTP request.
        return await runtime.model_operation("install", identifier, operation_key=operation_key(request))

    @app.post("/api/v1/models/{identifier}/verify")
    async def model_verify(identifier: str):
        return await settled_io(runtime.model_installer.verify, identifier)

    class ModelStart(BaseModel):
        model_config = ConfigDict(extra="forbid")
        runtime_id: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9_.-]{0,95}$")

    @app.post("/api/v1/models/{identifier}/start")
    async def model_start(identifier: str, request: Request, body: ModelStart | None = None):
        return await runtime.model_operation("start", identifier, operation_key=operation_key(request),
                                             runtime_id=body.runtime_id if body else None)

    @app.post("/api/v1/models/stop")
    async def model_stop(request: Request):
        return await runtime.model_operation("stop", operation_key=operation_key(request))

    @app.get("/api/v1/client/handshake")
    async def client_handshake():
        return runtime.client_handshake()

    @app.get("/api/v1/client/snapshot")
    async def client_snapshot():
        return runtime.store.client_snapshot(runtime.capabilities())

    @app.get("/api/v1/client/events")
    async def client_events(after: str = ""):
        return {"events": [runtime.store.client_snapshot(runtime.capabilities(), after=after)]}

    @app.get("/api/v1/client/receipts/{key:path}")
    async def client_receipt(key: str):
        return runtime.receipt(key)

    @app.get("/api/v1/resources")
    async def resources():
        return {"items": runtime.store.versions()}

    @app.get("/api/v1/resources/{resource_id}/versions")
    async def resource_versions(resource_id: str):
        return {"items": runtime.store.resource_versions(resource_id)}

    @app.post("/api/v1/resources/upload", status_code=202)
    async def upload(request: Request):
        key, filename = operation_key(request), upload_filename(request)
        with await body_file(request, MAX_INPUT) as stream:
            return runtime.upload_stream(stream, filename=filename, operation_key=key)

    @app.post("/api/v1/resources/{resource_id}/versions", status_code=202)
    async def append_version(resource_id: str, request: Request):
        # A new immutable version under the same logical resource. Earlier
        # versions are never rewritten; the new version parses independently
        # and keeps its own task, digest, size and identity.
        key, filename = operation_key(request), upload_filename(request)
        with await body_file(request, MAX_INPUT) as stream:
            return runtime.append_version_stream(
                resource_id, stream, filename=filename, operation_key=key)

    @app.delete("/api/v1/resources/{resource_id}", status_code=200)
    async def delete_resource(resource_id: str, request: Request):
        return runtime.store.resource_command(
            "resource.delete", resource_id,
            operation_key=operation_key(request), blobs=runtime.blobs)

    @app.get("/api/v1/tasks")
    async def tasks():
        return {"items": runtime.store.tasks()}

    @app.get("/api/v1/tasks/{task_id}")
    async def task(task_id: str):
        return runtime.store.task(task_id)

    @app.post("/api/v1/tasks/{task_id}/cancel")
    async def cancel(task_id: str):
        return runtime.store.cancel(task_id)

    @app.get("/api/v1/events")
    async def events(after: int = 0):
        return {"items": runtime.store.events(after)}

    @app.post("/api/v1/search")
    async def search(body: SearchInput):
        return runtime.search(**body.model_dump())

    @app.get("/api/v1/evidence/{evidence_id}")
    async def evidence(evidence_id: str):
        return runtime.store.evidence(evidence_id)

    @app.get("/api/v1/versions/{version_id}")
    async def version_detail(version_id: str):
        # Immutable projection: original bytes, digest, size and version
        # identity stay readable even when CPU parsing failed or a source
        # was withdrawn; only ready versions keep authorizing new queries.
        return runtime.store.version(version_id)

    @app.get("/api/v1/versions/{version_id}/bundle")
    async def export(version_id: str):
        return await asyncio.to_thread(runtime.download_bundle_response, version_id)

    @app.get("/api/v1/versions/{version_id}/source")
    async def original_source(version_id: str):
        return await asyncio.to_thread(runtime.download_source_response, version_id)

    @app.post("/api/v1/versions/{version_id}/withdraw", status_code=200)
    async def withdraw_version(version_id: str, request: Request):
        return runtime.store.resource_command("version.withdraw", version_id, operation_key=operation_key(request))

    @app.delete("/api/v1/versions/{version_id}", status_code=200)
    async def delete_version(version_id: str, request: Request):
        return runtime.store.resource_command(
            "version.delete", version_id,
            operation_key=operation_key(request), blobs=runtime.blobs)

    @app.post("/api/v1/bundles/import", status_code=201)
    async def import_bundle(request: Request):
        key = operation_key(request)
        with await body_file(request, MAX_ARCHIVE) as stream:
            return runtime.import_bundle(stream, operation_key=key)

    @app.post("/api/v1/answer")
    async def answer(body: GenerationInput, request: Request):
        keys = request.headers.getlist("idempotency-key")
        if len(keys) > 1:
            raise ApplicationError("invalid_key", "one unambiguous Idempotency-Key is required")
        return await runtime.answer(
            **body.model_dump(), operation_key=keys[0] if keys else None
        )

    @app.get("/api/v1/wikis")
    async def wiki_list(limit: int = 50, cursor: str | None = None):
        return runtime.wikis.list(limit=limit, cursor=cursor)

    @app.post("/api/v1/wikis", status_code=201)
    async def wiki_build(body: WikiBuild, request: Request):
        return await runtime.build_wiki(body.model_dump(), operation_key=operation_key(request))

    @app.get("/api/v1/wikis/{wiki_id}")
    async def wiki_get(wiki_id: str):
        return runtime.wikis.get(wiki_id)

    @app.get("/api/v1/wikis/{wiki_id}/revisions")
    async def wiki_revisions(wiki_id: str, limit: int = 50, cursor: str | None = None):
        return runtime.wikis.revisions(wiki_id, limit=limit, cursor=cursor)

    @app.get("/api/v1/wikis/{wiki_id}/revisions/{revision_id}")
    async def wiki_revision(wiki_id: str, revision_id: str):
        return runtime.wikis.get(wiki_id, revision_id)

    @app.post("/api/v1/wikis/{wiki_id}/revisions", status_code=201)
    async def wiki_rebuild(wiki_id: str, body: WikiRebuild, request: Request):
        return await runtime.build_wiki(body.model_dump(), wiki_id=wiki_id, operation_key=operation_key(request))

    @app.patch("/api/v1/wikis/{wiki_id}/pages/{page_key}", status_code=201)
    async def wiki_edit(wiki_id: str, page_key: str, body: WikiEdit, request: Request):
        return await runtime.edit_wiki(wiki_id, page_key, body.model_dump(), operation_key=operation_key(request))

    from ddp_local.plan_http import plan_router
    app.include_router(plan_router(runtime))
    from ddp_local.content_http import bundle_export_router, content_router
    app.include_router(content_router(runtime))
    app.include_router(bundle_export_router(runtime))
    app.add_middleware(RequestBodyBudget)
    return app
