"""Internal file-capability authority. Control owns tokens; corpus owns ACL and keys."""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from ddp_corpus.db import get_session
from ddp_corpus.deps import Actor, current_actor
from ddp_corpus.errors import APIError
from ddp_corpus.document_context import document_context
from ddp_corpus.policy import require_document

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
