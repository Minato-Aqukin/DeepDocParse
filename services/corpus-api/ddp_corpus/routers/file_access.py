"""Internal capability authorities. Control owns tokens and sessions; corpus owns ACL and keys."""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ddp_corpus.db import get_session
from ddp_corpus.deps import Actor, current_actor
from ddp_corpus.errors import APIError
from ddp_corpus.document_context import document_context
from ddp_corpus.policy import require_document
from ddp_corpus.resources import require_upload_target

router = APIRouter()


@router.get("/internal/file-access/{document_id}")
async def file_access(document_id: str, actor: Actor = Depends(current_actor),
                      session: AsyncSession = Depends(get_session)):
    document = await require_document(session, actor, document_id)
    if not document.object_key:
        raise APIError(404, "file not found", "invalid_request_error", "file_not_found")
    context = await document_context(session, actor, document)
    return {"document_id": document.id, "resource_id": context.resource_id or "",
            "object_key": document.object_key, "filename": context.filename,
            "mime": document.mime}


@router.get("/internal/upload-target/{resource_id}")
async def upload_target(resource_id: str,
                        sha256: str | None = Query(default=None, pattern="^[a-f0-9]{64}$"),
                        actor: Actor = Depends(current_actor),
                        session: AsyncSession = Depends(get_session)):
    """Admission for a byte upload that appends a version (DDP upload-control).

    Answered for the uploading actor before control allocates storage. The owner is
    `actor.id` because that is the `actor_id` control writes into DocumentSubmitted;
    the consumer re-runs this same predicate, so an allow here is never final.
    """
    if actor.kind not in ("user", "api_key"):
        raise APIError(404, "resource not found", "invalid_request_error", "resource_not_found")
    target = await require_upload_target(
        session, organization_id=actor.organization_id, owner_id=actor.id,
        resource_id=resource_id, source_digest=sha256)
    return {"resource_id": target.id}
