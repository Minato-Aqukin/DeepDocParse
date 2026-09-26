"""Center-content subset at center paths/shapes, over the local workspace.

Auth is the loopback session token (the boundary middleware); owner identity
is the single workspace owner. Upload part bytes live in the content-addressed
blob store; sessions only keep their keys. Persistence helpers live on
LocalStore (conversations/messages/assertions/citations, upload_sessions,
wiki_claim_bindings) and LocalRuntime (ask); this module only projects them
into center shapes.
"""

import json
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from ddp_contracts.enums import COMPILE_DEGRADED_VALUES, source_error_label
from ddp_core.application.ports import ApplicationError

MIME_ALLOWLIST = {
    "application/pdf",
    "application/octet-stream",
    "application/zip",
}

PART_SIZE = 5 * 1024 * 1024
SESSION_TTL = 24 * 3600
DOWNLOAD_TTL = 3600


def _iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def _unsupported():
    return JSONResponse(
        {"error": {"code": "not_supported_locally",
                   "message": source_error_label("not_supported_locally")}}, 404,
    )


def _version_state_to_parse(version):
    return {"queued": "pending", "parsing": "running", "ready": "succeeded",
            "failed": "failed", "withdrawn": "failed", "unparsed": "pending"}.get(
                version["state"], "pending")


def _version_state_to_index(version):
    return {"queued": "pending", "parsing": "indexing", "ready": "ready",
            "failed": "failed", "withdrawn": "failed", "unparsed": "none"}.get(
                version["state"], "none")


def _page_count(runtime, version):
    if not version.get("layout_key"):
        return 0
    try:
        layout = json.loads(runtime.blobs.read(version["layout_key"], 32 * 1024 * 1024))
    except Exception:
        return 0
    pages = layout.get("layout", layout).get("pdf_info", []) if isinstance(layout, dict) else []
    indices = {p.get("page_idx", i) for i, p in enumerate(pages) if isinstance(p, dict)}
    return len(indices)


def _resource_out(runtime, resource_id):
    resource = runtime.store.resource(resource_id)
    versions = runtime.store.resource_versions(resource_id)
    items = []
    for number, version in enumerate(sorted(versions, key=lambda v: v["created_at"]), start=1):
        items.append({
            "id": version["id"], "resource_id": version["resource_id"],
            "version_no": number, "document_id": version["id"],
            "source_digest": version["source_digest"],
            "source_digest_verified": True,
            "filename": version["filename"], "size_bytes": version["size_bytes"],
            "parse_job_id": version["parse_revision"],
            "parse_status": _version_state_to_parse(version),
            "index_status": _version_state_to_index(version),
            "created_at": _iso(version["created_at"]),
        })
    return {"id": resource["id"],
            "organization_id": "local",
            "owner_id": "workspace:" + runtime.store.workspace_id,
            "uploader_ref": {"issuer": runtime.store.environment_id,
                             "subject": "workspace:" + runtime.store.workspace_id},
            "display_name": resource["title"], "publication": "private",
            "copied_from": None, "created_at": _iso(resource["created_at"]),
            "updated_at": _iso(resource["created_at"]), "versions": items}


def _document_out(runtime, version, version_no=None):
    provider = version.get("provider") or {}
    # The local version record also lists embedding/vision unavailability; those are not
    # compile degradations (content-v1 types this field as the compile_degraded enum) —
    # the keyword-only search reports embedding_unavailable on its own `degraded` field.
    degraded = [item for item in version.get("degraded") or [] if item in COMPILE_DEGRADED_VALUES]
    layout_key = version.get("layout_key")
    layout_version, code_detection = "", "unavailable"
    if layout_key:
        try:
            raw = json.loads(runtime.blobs.read(layout_key, 32 * 1024 * 1024))
            layout = raw.get("layout", raw) if isinstance(raw, dict) else {}
            layout_version = str(layout.get("layout_version") or "")
            code_detection = str(layout.get("code_detection") or "unavailable")
        except Exception:
            pass
    from ddp_core.compilation import fingerprint as _fingerprint

    compile_fingerprint = _fingerprint({
        "layout_engine": provider.get("layout_engine", "borndigital"),
        "layout_version": layout_version,
        "parse_options_hash": provider.get("parse_options_hash", ""),
        "compiler": provider.get("compiler", "ddp-compile/1"),
        "chunker": provider.get("chunker", "ddp-chunk/3"),
        "tokenizer": provider.get("tokenizer", ""),
        "embedding_model": provider.get("embedding_model", "disabled"),
        "vision_model": provider.get("vision_model", "disabled"),
        "provider_resolved": provider.get("provider_resolved", False),
    }) if layout_key else ""
    compile_status = {"ready": "ready", "parsing": "compiling", "queued": "pending",
                      "failed": "failed"}.get(version["state"], "pending")
    status = _version_state_to_parse(version)
    return {
        "id": version["id"], "resource_id": version["resource_id"],
        "source_version_id": version["id"], "source_version_no": version_no,
        "filename": version["filename"], "doc_id": version["source_digest"],
        "origin": "local", "mime": "application/pdf", "size_bytes": version["size_bytes"],
        "page_count": _page_count(runtime, version), "status": status,
        "error": None if status == "succeeded" else version.get("error"),
        "index_status": _version_state_to_index(version),
        "index_error": None if version["state"] == "ready" else version.get("error"),
        "compile_status": compile_status, "compile_degraded": degraded,
        "compile_fingerprint": compile_fingerprint, "layout_version": layout_version,
        "code_detection": code_detection, "current_job_id": version["parse_revision"],
        "created_at": _iso(version["created_at"]),
        "uploaders": ["owner"], "can_delete": True,
    }


def _chunk_row(runtime, evidence_id):
    row = runtime.store.evidence(evidence_id)
    with runtime.store.lock:
        raw = runtime.store.db.execute(
            "SELECT chunk FROM evidence WHERE id=?", (evidence_id,)).fetchone()[0]
    return row, json.loads(raw)


def _citation_out(runtime, version, evidence_id, *, rank=0, score=None, similarity=None):
    row, chunk = _chunk_row(runtime, evidence_id)
    bbox = chunk.get("bbox")
    page_size = chunk.get("page_size")
    return {
        "evidence_id": evidence_id, "source_type": "source", "derived_from": None,
        "chunk_id": evidence_id, "parse_job_id": version["parse_revision"],
        "seq": chunk.get("seq"), "page_idx": chunk.get("page_idx", 0),
        "bbox": bbox, "page_size": page_size, "crop_url": None,
        "snippet": " ".join((row["excerpt"] or "").split())[:200],
        "score": score if score is not None else 0.0, "similarity": similarity,
        "resolved": True,
    }


def _evidence_detail_out(runtime, version, evidence_id):
    row, chunk = _chunk_row(runtime, evidence_id)
    bbox = chunk.get("bbox")
    page_size = chunk.get("page_size")
    return {
        "id": evidence_id, "resource_id": version["resource_id"],
        "source_version_id": version["id"], "source_digest": version["source_digest"],
        "parse_revision": version["parse_revision"],
        "document": {"id": version["id"], "filename": version["filename"]},
        "page_idx": chunk.get("page_idx", 0), "seq": chunk.get("seq", 0),
        "parse_job_id": version["parse_revision"], "doc_version": 1,
        "bbox": bbox, "page_size": page_size, "kind": chunk.get("block_type", "text"),
        "content": row["excerpt"], "source_type": "source", "derived_from": None,
        "crop_url": None, "review_state": "unreviewed", "chunk_id": evidence_id,
        "verifications": [],
    }


def _message_out(runtime, message):
    assertions = runtime.store.assertions_for_message(message["id"])
    payloads = []
    for assertion in assertions:
        citations = []
        raw_ids = assertion["evidence_ids"]
        ids = json.loads(raw_ids) if isinstance(raw_ids, str) else raw_ids
        for index, evidence_id in enumerate(ids):
            row = runtime.store.evidence(evidence_id)
            version = runtime.store.version(row["version_id"])
            citations.append(_citation_out(
                runtime, version, evidence_id, rank=index,
                score=0.0, similarity=None))
        verification = {"state": assertion["verification_state"],
                        "mode": assertion["verification_mode"]}
        payloads.append({
            "id": assertion["id"], "position": assertion["position"],
            "text": assertion["text"],
            "evidence_ids": (json.loads(assertion["evidence_ids"])
                             if isinstance(assertion["evidence_ids"], str)
                             else assertion["evidence_ids"]),
            "verification": verification,
            "unsupported": bool(assertion["unsupported"]), "citations": citations,
        })
    union, seen = [], set()
    for assertion in payloads:
        for citation in assertion["citations"]:
            if citation["evidence_id"] not in seen:
                seen.add(citation["evidence_id"])
                union.append(citation)
    query_decision = json.loads(message["query_json"] or "{}") or None
    retrieval = None
    confidence = json.loads(message["confidence"]) if message["confidence"] else None
    return {
        "id": message["id"], "role": message["role"], "content": message["content"],
        "citations": union, "assertions": payloads,
        **({"query_decision": query_decision} if query_decision else {}),
        **({"retrieval": retrieval} if retrieval is not None else {}),
        "verified": bool(message["verified"]), "degraded": message["degraded"],
        "model_meta": json.loads(message["model_meta"] or "{}"),
        **({"confidence": confidence} if confidence is not None else {}),
        "created_at": _iso(message["created_at"]),
    }


def _session_out(session):
    parts = json.loads(session["parts_json"])
    total = session["declared_size"]
    count = max(1, (total + PART_SIZE - 1) // PART_SIZE) if total else 1
    known = {int(number) for number in parts}
    missing = [{"part_number": number,
                "url": f"/api/uploads/{session['id']}/parts/{number}"}
               for number in range(1, count + 1) if number not in known]
    completed = [{"part_number": number, "etag": parts[str(number)]["blob_key"],
                  "size": parts[str(number)]["size"]}
                 for number in sorted(known)]
    status = {"uploading": "uploading", "created": "created", "ready": "ready",
              "verifying": "verifying", "failed": "failed"}.get(session["status"], "created")
    ingest = ("ready" if session["status"] == "ready"
              else "rejected" if session["status"] == "failed" else "pending")
    return {
        "id": session["id"], "status": status, "object_key": "local:" + session["id"],
        "filename": session["filename"], "mime": session["mime"],
        "declared_size": session["declared_size"], "part_size": PART_SIZE,
        "allocation_state": "ready", "parts": missing, "completed_parts": completed,
        "expires_at": _iso(session["created_at"] + SESSION_TTL),
        **({"error": session["error"]} if session["error"] else {}),
        "target_resource_id": session["target_resource_id"],
        "ingest_status": ingest,
        "ingest_error": None,
        **({"resource_id": session["resource_id"]}
           if session.get("resource_id") else {}),
        **({"version_id": session["version_id"]}
           if session.get("version_id") else {}),
    }


def _sse(event, payload):
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()


def content_router(runtime):
    router = APIRouter()

    class CreateUpload(BaseModel):
        model_config = ConfigDict(extra="forbid")
        filename: str = Field(min_length=1, max_length=255)
        size: int = Field(ge=1)
        mime: str = Field(min_length=1, max_length=255)
        sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
        target_resource_id: str | None = None

    class AskIn(BaseModel):
        model_config = ConfigDict(extra="forbid")
        question: str = Field(min_length=1, max_length=2000)

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

    def content_key(request):
        key = request.headers.get("idempotency-key")
        if not key:
            raise ApplicationError("invalid_key", "Idempotency-Key is required")
        return key

    def version_number(version_id):
        versions = runtime.store.resource_versions(
            runtime.store.version(version_id)["resource_id"])
        ordered = sorted(versions, key=lambda v: v["created_at"])
        return next(i for i, v in enumerate(ordered, start=1) if v["id"] == version_id)

    @router.get("/api/auth/me")
    async def auth_me():
        return {"id": "workspace:" + runtime.store.workspace_id, "username": "owner",
                "email": None, "role": "admin", "organization_id": "local",
                "created_at": _iso(time.time())}

    @router.get("/api/resources")
    async def resources_list(scope: str = "mine", offset: int = 0, limit: int = 50):
        if scope not in {"mine", "site_public"}:
            raise ApplicationError("invalid_scope", "scope must be mine or site_public")
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 200:
            raise ApplicationError("invalid_window", "offset/limit window is invalid")
        if scope == "site_public":
            return {"items": [], "offset": offset, "limit": limit, "has_more": False,
                    "coverage": {"scope": scope, "complete": True, "watermark": None,
                                 "snapshot_complete": True}}
        with runtime.store.lock:
            rows = runtime.store.db.execute(
                "SELECT * FROM resources ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?",
                (limit + 1, offset)).fetchall()
        more = len(rows) > limit
        items = [_resource_out(runtime, row["id"]) for row in rows[:limit]]
        watermark = max((item["updated_at"] for item in items), default=None)
        return {"items": items, "offset": offset, "limit": limit, "has_more": more,
                "coverage": {"scope": scope, "complete": not more, "watermark": watermark,
                             "snapshot_complete": True}}

    @router.get("/api/resources/{resource_id}")
    async def resources_get(resource_id: str):
        return _resource_out(runtime, resource_id)

    @router.delete("/api/resources/{resource_id}", status_code=204)
    async def resources_delete(resource_id: str, request: Request):
        runtime.store.resource_command(
            "resource.delete", resource_id, operation_key=content_key(request))
        return Response(status_code=204)

    @router.post("/api/uploads", status_code=201)
    async def uploads_create(body: CreateUpload, request: Request):
        if body.mime not in MIME_ALLOWLIST:
            raise ApplicationError("unsupported_mime", "local uploads accept PDF, octet-stream or zip")
        if body.size > 32 * 1024 * 1024:
            raise ApplicationError("input_too_large", "upload exceeds the 32 MiB local budget")
        session = runtime.store.create_upload_session(
            filename=runtime.filename(body.filename), mime=body.mime,
            declared_size=body.size, declared_sha256=body.sha256,
            target_resource_id=body.target_resource_id,
            idempotency_key=request.headers.get("idempotency-key"))
        created = session["created_at"] == session["updated_at"]
        return JSONResponse(_session_out(session), 201 if created else 200)

    @router.get("/api/uploads/{upload_id}")
    async def uploads_get(upload_id: str):
        return _session_out(runtime.store.upload_session(upload_id))

    @router.put("/api/uploads/{upload_id}/parts/{part_number}")
    async def uploads_part(upload_id: str, part_number: int, request: Request):
        body = await request.body()
        if len(body) > 8 * 1024 * 1024:
            raise ApplicationError("input_too_large", "upload part exceeds the 8 MiB part budget")
        key, size = runtime.blobs.put_bytes(body)
        runtime.store.store_upload_part(upload_id, part_number, key, size)
        return {"part_number": part_number, "etag": key, "size": size}

    @router.post("/api/uploads/{upload_id}/finalize")
    async def uploads_finalize(upload_id: str):
        session = runtime.store.finalize_upload_session(
            upload_id, runtime.blobs.read, runtime.blobs.put_bytes)
        return _session_out(session)

    @router.get("/api/documents")
    async def documents_list(q: str = "", status: str = "", limit: int = 50, offset: int = 0):
        versions = runtime.store.versions()
        rows = []
        for version in versions:
            info = _document_out(runtime, version, version_number(version["id"]))
            if q and q.lower() not in info["filename"].lower():
                continue
            if status and info["status"] != status:
                continue
            rows.append(info)
        return rows[max(0, offset):max(0, offset) + max(1, min(limit, 200))]

    @router.get("/api/documents/stats/summary")
    async def documents_stats():
        versions = runtime.store.versions()
        pages = sum(_page_count(runtime, v) for v in versions)
        askable = sum(1 for v in versions if v["state"] == "ready")
        return {"documents": len(versions), "pages": pages, "askable": askable}

    @router.get("/api/documents/{document_id}")
    async def documents_get(document_id: str):
        version = runtime.store.version(document_id)
        return _document_out(runtime, version, version_number(version["id"]))

    @router.delete("/api/documents/{document_id}", status_code=204)
    async def documents_delete(document_id: str, request: Request):
        version = runtime.store.version(document_id)
        runtime.store.resource_command(
            "resource.delete", version["resource_id"],
            operation_key=content_key(request))
        return Response(status_code=204)

    @router.get("/api/documents/{document_id}/jobs")
    async def documents_jobs(document_id: str):
        version = runtime.store.version(document_id)
        with runtime.store.lock:
            tasks = [dict(row) for row in runtime.store.db.execute(
                "SELECT * FROM tasks WHERE version_id=? ORDER BY created_at DESC,id DESC",
                (version["id"],)).fetchall()]
        jobs = []
        for task in tasks:
            jobs.append({
                "id": task["id"], "engine": "borndigital", "options": {},
                "status": _version_state_to_parse(version),
                "error": version.get("error"), "page_count": _page_count(runtime, version),
                "is_current": True, "created_at": _iso(task["created_at"]),
                "archived_at": None, "document_version": 1,
            })
        if not jobs:
            jobs.append({
                "id": version["parse_revision"], "engine": "borndigital", "options": {},
                "status": _version_state_to_parse(version),
                "error": version.get("error"), "page_count": _page_count(runtime, version),
                "is_current": True, "created_at": _iso(version["created_at"]),
                "archived_at": None, "document_version": 1,
            })
        return jobs

    def _require_layout(version):
        if not version.get("layout_key"):
            raise ApplicationError("result_not_ready", "parse has not produced output yet")
        try:
            return json.loads(runtime.blobs.read(version["layout_key"], 32 * 1024 * 1024))
        except Exception as exc:
            raise ApplicationError("result_not_ready", "stored layout is unreadable") from exc

    @router.get("/api/documents/{document_id}/pages")
    async def documents_pages(document_id: str, job: str = ""):
        version = runtime.store.version(document_id)
        if job and job != version["parse_revision"]:
            raise ApplicationError("not_found", "parse job not found in this workspace")
        rows = runtime.store.db.execute(
            "SELECT * FROM evidence WHERE version_id=? ORDER BY seq,id",
            (version["id"],)).fetchall()
        if not rows:
            _require_layout(version)
        pages: dict[int, list] = {}
        for row in rows:
            chunk = json.loads(row["chunk"])
            pages.setdefault(chunk.get("page_idx", 0), []).append({
                "chunk_id": row["id"], "seq": chunk.get("seq", 0),
                "page_idx": chunk.get("page_idx", 0), "bbox": chunk.get("bbox"),
                "page_size": chunk.get("page_size"), "text": row["excerpt"]})
        ordered = sorted(pages.items())
        first_blocks = ordered[0][1] if ordered else []
        page_size = first_blocks[0]["page_size"] if first_blocks else None
        return {"document_id": version["id"], "job_id": version["parse_revision"],
                "page_count": max((index for index, _ in ordered), default=-1) + 1
                if ordered else _page_count(runtime, version),
                "pages": [{"page_idx": index, "page_size": page_size, "blocks": blocks}
                          for index, blocks in ordered]}

    @router.get("/api/documents/{document_id}/layout")
    async def documents_layout(document_id: str, job: str = ""):
        version = runtime.store.version(document_id)
        if job and job != version["parse_revision"]:
            raise ApplicationError("not_found", "parse job not found in this workspace")
        return _require_layout(version)

    @router.get("/api/documents/{document_id}/result")
    async def documents_result(document_id: str, job: str = ""):
        from ddp_core.application.borndigital import to_markdown

        version = runtime.store.version(document_id)
        if job and job != version["parse_revision"]:
            raise ApplicationError("not_found", "parse job not found in this workspace")
        wrapped = _require_layout(version)
        layout = wrapped.get("layout", wrapped) if isinstance(wrapped, dict) else {}
        pages = []
        for page in layout.get("pdf_info", []):
            blocks = []
            for block in page.get("para_blocks", []):
                text = " ".join(span.get("content", "")
                                for line in block.get("lines", [])
                                for span in line.get("spans", [])).strip()
                if text:
                    blocks.append({"bbox": block.get("bbox"), "text": text})
            pages.append({"page_idx": page.get("page_idx", 0),
                          "page_size": page.get("page_size"), "blocks": blocks})
        return {"document_id": version["id"], "job_id": version["parse_revision"],
                "filename": version["filename"],
                "page_count": _page_count(runtime, version),
                "markdown": to_markdown(pages), "images": []}

    @router.get("/api/documents/{document_id}/download-url")
    async def documents_download_url(document_id: str, disposition: str = "inline"):
        version = runtime.store.version(document_id)
        if disposition not in {"inline", "attachment"}:
            raise ApplicationError("invalid_disposition", "disposition must be inline or attachment")
        return {"url": f"/api/documents/{version['id']}/source",
                "expires_at": _iso(time.time() + DOWNLOAD_TTL), "supports_range": False}

    @router.get("/api/documents/{document_id}/source")
    async def documents_source(document_id: str):
        return Response(await _async_source_bytes(document_id), media_type="application/pdf",
                        headers={"Content-Disposition": 'inline; filename="document.pdf"'})

    async def _async_source_bytes(document_id):
        import asyncio as _asyncio

        return await _asyncio.to_thread(runtime.source_bytes, document_id)

    @router.get("/api/search")
    async def search(q: str = "", doc: str = "", limit: int = 20):
        if not q.strip():
            return {"query": q, "groups": []}
        version_ids = None
        if doc:
            version = runtime.store.version(doc)
            version_ids = [version["id"]]
        found = runtime.application.search(q, version_ids=version_ids,
                                           limit=max(1, min(limit, 50)))
        groups: dict[str, dict] = {}
        for hit in found["hits"]:
            version = runtime.store.version(hit["version_id"])
            key = version["id"]
            group = groups.setdefault(key, {
                "document_id": version["id"], "resource_id": version["resource_id"],
                "source_version_id": version["id"],
                "source_version_no": version_number(version["id"]),
                "parse_revision": version["parse_revision"],
                "filename": version["filename"], "hits": []})
            group["hits"].append({
                "chunk_id": hit["evidence_id"], "page_idx": hit.get("page_idx", 0),
                "bbox": hit.get("bbox"), "score": hit.get("score", 0.0),
                "similarity": hit.get("similarity"),
                "snippet": " ".join((hit.get("text") or "").split())[:200]})
        return {"query": q, "degraded": found["degraded"][0] if found["degraded"] else None,
                "groups": list(groups.values())}

    @router.post("/api/documents/{document_id}/conversations", status_code=201)
    async def conversations_create(document_id: str):
        version = runtime.store.version(document_id)
        conversation = runtime.store.create_conversation(
            version["id"], resource_id=version["resource_id"])
        return {"id": conversation["id"], "document_id": version["id"],
                "title": conversation["title"], "created_at": _iso(conversation["created_at"]),
                "updated_at": _iso(conversation["updated_at"])}

    @router.get("/api/conversations")
    async def conversations_list(document: str = ""):
        if document:
            runtime.store.version(document)
            rows = runtime.store.conversations_for_document(document)
        else:
            with runtime.store.lock:
                rows = [dict(row) for row in runtime.store.db.execute(
                    "SELECT * FROM conversations ORDER BY updated_at DESC,id DESC").fetchall()]
        return [{"id": row["id"], "document_id": row["document_id"], "title": row["title"],
                 "created_at": _iso(row["created_at"]), "updated_at": _iso(row["updated_at"])}
                for row in rows]

    @router.get("/api/conversations/{conversation_id}/messages")
    async def conversations_messages(conversation_id: str):
        return [_message_out(runtime, message)
                for message in runtime.store.conversation_messages(conversation_id)]

    @router.delete("/api/conversations/{conversation_id}", status_code=204)
    async def conversations_delete(conversation_id: str):
        runtime.store.delete_conversation(conversation_id)
        return Response(status_code=204)

    @router.post("/api/conversations/{conversation_id}/ask")
    async def conversations_ask(conversation_id: str, body: AskIn):
        conversation = runtime.store.conversation(conversation_id)
        version = runtime.store.version(conversation["document_id"])
        if version["state"] != "ready":
            raise ApplicationError("index_not_ready", "document is not ready for questions yet")

        async def stream():
            try:
                result = await runtime.ask(conversation_id, body.question)
            except ApplicationError as exc:
                yield _sse("error", {"message": str(exc), "code": exc.code})
                return
            citations = [_citation_out(
                runtime, runtime.store.version(
                    runtime.store.evidence(item["evidence_id"])["version_id"]),
                item["evidence_id"], rank=index, score=item.get("score"),
                similarity=item.get("similarity"))
                for index, item in enumerate(result["citations"])]
            candidates = result["decisions"]
            chunk_ids = [c["chunk_id"] for c in citations]
            yield _sse("meta", {
                "query_decision": result["query_decision"],
                "retrieval": {"chunk_ids": chunk_ids, "candidates": candidates}})
            # Documented single-delta emission: the local model produces the
            # whole answer at once; the frontend concatenates delta frames.
            text = "\n".join(a["text"] for a in result["assertions"])
            yield _sse("delta", {"text": text})
            yield _sse("citations", {"citations": citations})
            assertions = [{
                "id": None, "position": a.get("position", index), "text": a["text"],
                "evidence_ids": a["evidence_ids"],
                "verification": {"state": "unverified", "mode": None},
                "unsupported": a["unsupported"], "citations": [
                    c for c in citations if c["evidence_id"] in set(a["evidence_ids"])],
            } for index, a in enumerate(result["assertions"])]
            yield _sse("assertions", {"assertions": assertions})
            stored = runtime.store.conversation_messages(conversation_id)[-1]
            yield _sse("done", {"message_id": stored["id"], "verified": False,
                                "degraded": result["degraded"][0] if result["degraded"] else None,
                                "confidence": result["confidence"]})

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    @router.get("/api/evidence/{evidence_id}")
    async def evidence_get(evidence_id: str):
        row = runtime.store.evidence(evidence_id)
        version = runtime.store.version(row["version_id"])
        return _evidence_detail_out(runtime, version, evidence_id)

    @router.get("/api/evidence/{evidence_id}/backlinks")
    async def evidence_backlinks(evidence_id: str):
        runtime.store.evidence(evidence_id)
        backlinks = []
        for row in runtime.store.backlinks_for_evidence(evidence_id):
            backlinks.append({"source_kind": "assertion", "source_id": row["assertion_id"],
                              "role": "primary", "label": row["label"]})
        for row in runtime.store.wiki_claims_for_evidence(evidence_id):
            current = runtime.wikis.get(row["wiki_id"])["revision"]
            if current["id"] != row["revision_id"]:
                continue
            backlinks.append({"source_kind": "wiki_claim", "source_id": row["claim_id"],
                              "role": "primary",
                              "label": next((claim["text"] for page in current["pages"]
                                             for section in page.get("generated_sections", [])
                                             for claim in section.get("sentences", [])
                                             if claim["id"] == row["claim_id"]), row["claim_id"]),
                              "wiki_id": row["wiki_id"], "wiki_title": row["wiki_title"],
                              "revision_id": row["revision_id"]})
        return {"evidence_id": evidence_id, "backlinks": backlinks}

    @router.get("/api/wikis")
    async def wikis_list():
        with runtime.store.lock:
            rows = runtime.store.db.execute(
                "SELECT * FROM wikis ORDER BY created_at DESC,id DESC LIMIT 200").fetchall()
        return [runtime.wikis.get(row["id"]) for row in rows]

    @router.post("/api/wikis", status_code=201)
    async def wikis_create(body: WikiBuild, request: Request):
        result = await runtime.build_wiki(body.model_dump(),
                                          operation_key=content_key(request))
        _record_bindings(result)
        return _wiki_out(result)

    @router.get("/api/wikis/{wiki_id}")
    async def wikis_get(wiki_id: str):
        return _wiki_out(runtime.wikis.get(wiki_id))

    @router.get("/api/wikis/{wiki_id}/revisions/{revision_id}")
    async def wikis_revision(wiki_id: str, revision_id: str):
        return _wiki_out(runtime.wikis.get(wiki_id, revision_id))

    @router.post("/api/wikis/{wiki_id}/revisions", status_code=201)
    async def wikis_rebuild(wiki_id: str, body: WikiRebuild, request: Request):
        result = await runtime.build_wiki(body.model_dump(), wiki_id=wiki_id,
                                          operation_key=content_key(request))
        _record_bindings(result)
        return _wiki_out(result)

    @router.patch("/api/wikis/{wiki_id}/pages/{page_key}", status_code=201)
    async def wikis_edit(wiki_id: str, page_key: str, body: WikiEdit, request: Request):
        return _wiki_out(await runtime.edit_wiki(
            wiki_id, page_key, body.model_dump(), operation_key=content_key(request)))

    def _record_bindings(result):
        frozen_by_id = {}
        for dep in result["revision"].get("dependency_manifest", []):
            try:
                current = runtime.store.evidence(dep["evidence_id"])
            except ApplicationError:
                continue
            frozen_by_id[dep["evidence_id"]] = current
        if frozen_by_id:
            with runtime.store.tx():
                runtime.store.record_wiki_claim_bindings(
                    result["revision"]["id"], result["wiki"]["id"],
                    result["revision"]["pages"], frozen_by_id)

    def _wiki_out(result):
        revision = dict(result["revision"])
        revision["created_at"] = _iso(revision["created_at"])
        return {"wiki": result["wiki"], "revision": revision}

    # Explicitly unsupported center operations used by the Web shell.
    @router.patch("/api/resources/{resource_id}")
    async def resources_patch(resource_id: str):
        _ = resource_id
        return _unsupported()

    @router.post("/api/wikis/{wiki_id}/publish")
    async def wikis_publish(wiki_id: str):
        _ = wiki_id
        return _unsupported()

    for _path, _methods in [
        ("/api/documents/{document_id}/reparse", ["POST"]),
        ("/api/documents/{document_id}/reindex", ["POST"]),
        ("/api/documents/{document_id}/validate-index", ["POST"]),
        ("/api/documents/{document_id}/current-job", ["PUT"]),
        ("/api/documents/{document_id}/download", ["GET"]),
        ("/api/documents/{document_id}/source-url", ["GET"]),
        ("/api/evidence/{evidence_id}/verification", ["POST"]),
    ]:
        async def _unsupported_route(request: Request):
            _ = request
            return _unsupported()

        router.add_api_route(_path, _unsupported_route, methods=_methods)

    async def _crop_fallback(request: Request):
        _ = request
        return _unsupported()

    router.add_api_route("/api/documents/{document_id}/crops/{job_id}/{name}",
                         _crop_fallback, methods=["GET"])
    return router


def bundle_export_router(runtime):
    """Bundle export per bundle-v1 (resource/version path)."""
    router = APIRouter()

    @router.get("/api/resources/{resource_id}/versions/{version_id}/bundle")
    async def bundle_export(resource_id: str, version_id: str):
        version = runtime.store.version(version_id)
        if version["resource_id"] != resource_id:
            raise ApplicationError("not_found", "version not found in this resource")
        return Response(runtime.export_bundle(version_id), media_type="application/zip",
                        headers={"Content-Disposition": 'attachment; filename="document.ddp.zip"'})

    @router.get("/api/resources/{resource_id}/versions/{version_id}/bundle/evidence")
    async def bundle_evidence(resource_id: str, version_id: str):
        from ddp_core.bundle import read_bundle
        import io as _io

        version = runtime.store.version(version_id)
        if version["resource_id"] != resource_id:
            raise ApplicationError("not_found", "version not found in this resource")
        verified = read_bundle(_io.BytesIO(runtime.export_bundle(version_id)))
        manifest = verified.manifest or {}
        structural = manifest.get("structural_validation", "valid")
        semantic = manifest.get("semantic_review", "needs_review")
        if isinstance(structural, dict):
            structural = structural.get("status", "valid")
        if isinstance(semantic, dict):
            semantic = semantic.get("status", "needs_review")
        return {"source": verified.source,
                "evidence": [{"evidence": record["evidence"], "excerpt": record["excerpt"]}
                             for record in verified.evidence],
                "structural_validation": structural, "semantic_review": semantic}

    return router
