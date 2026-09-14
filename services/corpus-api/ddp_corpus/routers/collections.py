"""Owner/admin publication API and service-authenticated collection directory producer."""
import re
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, AwareDatetime, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from ddp_contracts import ROLE_VALUES
from ddp_corpus import catalog
from ddp_corpus.db import get_session
from ddp_corpus.deps import Actor, current_actor, require_service_actor
from ddp_corpus.errors import APIError, error_body

router = APIRouter()
Tag = Annotated[str, Field(min_length=1, max_length=128)]
VersionID = Annotated[str, Field(min_length=1, max_length=32)]


class TimeRange(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    from_: AwareDatetime | None = Field(default=None, alias="from")
    to: AwareDatetime | None = None

    @model_validator(mode="after")
    def ordered(self):
        if self.from_ and self.to and self.from_ > self.to:
            raise ValueError("time_range must be ordered")
        return self


class CollectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=255)
    licence: str = Field(min_length=1, max_length=512)
    languages: list[Tag] = Field(default_factory=list, max_length=30)
    topics: list[Tag] = Field(default_factory=list, max_length=50)
    time_range: TimeRange | None = None
    version_ids: list[VersionID] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def distinct(self):
        if len(self.version_ids) != len(set(self.version_ids)):
            raise ValueError("version_ids must be distinct")
        return self


class ReplaceCollection(CollectionInput):
    expected_revision: int = Field(ge=1)


class RevisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)


def response(value, status=200):
    return JSONResponse(jsonable_encoder(value), status_code=status, headers={"Cache-Control": "no-store"})


async def write(session, actor, operation, body, key, collection_id=None):
    try:
        result = await catalog.mutate(session, actor, operation,
            body.model_dump(mode="json", by_alias=True, exclude_none=True), key, collection_id)
        return response(result, 201 if operation == "create" else 200)
    except Exception:
        await session.rollback()
        raise


@router.post("/api/v1/collections", status_code=201)
async def create(body: CollectionInput, actor: Actor = Depends(current_actor),
                 session: AsyncSession = Depends(get_session),
                 idempotency_key: str = Header(min_length=1, max_length=128)):
    return await write(session, actor, "create", body, idempotency_key)


@router.get("/api/v1/collections/{collection_id}")
async def get(collection_id: str, actor: Actor = Depends(current_actor),
              session: AsyncSession = Depends(get_session)):
    row = await catalog.require_collection(session, actor, collection_id)
    return response(await catalog.collection_output(session, actor, row))


@router.put("/api/v1/collections/{collection_id}")
async def replace(collection_id: str, body: ReplaceCollection, actor: Actor = Depends(current_actor),
                  session: AsyncSession = Depends(get_session),
                  idempotency_key: str = Header(min_length=1, max_length=128)):
    return await write(session, actor, "replace", body, idempotency_key, collection_id)


@router.post("/api/v1/collections/{collection_id}/publish")
async def publish(collection_id: str, body: RevisionInput, actor: Actor = Depends(current_actor),
                  session: AsyncSession = Depends(get_session),
                  idempotency_key: str = Header(min_length=1, max_length=128)):
    return await write(session, actor, "publish", body, idempotency_key, collection_id)


@router.post("/api/v1/collections/{collection_id}/withdraw")
async def withdraw(collection_id: str, body: RevisionInput, actor: Actor = Depends(current_actor),
                   session: AsyncSession = Depends(get_session),
                   idempotency_key: str = Header(min_length=1, max_length=128)):
    return await write(session, actor, "withdraw", body, idempotency_key, collection_id)


async def caller_context(request: Request, service: Actor = Depends(require_service_actor)):
    actor_id = request.headers.get("X-DDP-Caller-Actor", "")
    kind = request.headers.get("X-DDP-Caller-Kind", "")
    role = request.headers.get("X-DDP-Caller-Role", "")
    user_id = request.headers.get("X-DDP-Caller-User", "")
    scope = request.headers.get("X-DDP-Caller-Scope", "")
    origin = request.headers.get("X-DDP-Authority-Node", "")
    if (not actor_id or len(actor_id) > 128 or kind not in ("user", "api_key")
            or role not in ROLE_VALUES or (kind == "api_key" and not user_id)
            or len(user_id) > 128 or not re.fullmatch(r"sha256:[0-9a-f]{64}", scope)
            or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,63}", origin)):
        raise catalog.error("missing_caller_context", 401)
    return Actor(id=actor_id, kind=kind, organization_id=service.organization_id,
        role=role, user_id=user_id or None), scope, origin


@router.get("/internal/federation/collections")
async def enumerate_collections(scope_id: str = Query(min_length=1, max_length=128),
        snapshot_id: str = Query(default="", max_length=32),
        cursor: str = Query(default="", max_length=32),
        limit: int | None = Query(default=None, ge=1, le=100),
        context=Depends(caller_context), session: AsyncSession = Depends(get_session)):
    actor, caller_scope, origin = context
    try:
        return response(await catalog.snapshot_page(session, actor, scope_id, caller_scope,
            origin, snapshot_id, cursor, limit))
    except APIError as exc:
        revoked = getattr(exc, "revoked_collection_ids", None)
        if revoked is not None:
            return response({**error_body(str(exc.detail), exc.type, exc.code),
                             "revoked_collection_ids": revoked}, 410)
        raise


@router.get("/internal/federation/published-collections")
async def enumerate_published_collections(snapshot_id: str = Query(default="", max_length=32),
        cursor: str = Query(default="", max_length=32),
        limit: int | None = Query(default=None, ge=1, le=100),
        service: Actor = Depends(require_service_actor),
        session: AsyncSession = Depends(get_session)):
    """Peer directory producer: service-only, no caller-supplied scope.

    The fixed node scope and origin are derived in `catalog.published_snapshot_page`;
    the existing `/internal/federation/collections` caller-scoped path is untouched.
    """
    try:
        return response(await catalog.published_snapshot_page(session, service,
            snapshot_id, cursor, limit))
    except APIError as exc:
        revoked = getattr(exc, "revoked_collection_ids", None)
        if revoked is not None:
            return response({**error_body(str(exc.detail), exc.type, exc.code),
                             "revoked_collection_ids": revoked}, 410)
        raise
