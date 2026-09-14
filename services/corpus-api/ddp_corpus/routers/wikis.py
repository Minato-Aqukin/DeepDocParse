"""Versioned Wiki HTTP workflow; contract: packages/contracts/openapi/wiki-v1.yaml."""
import httpx
from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ddp_corpus import wiki
from ddp_corpus.db import get_session
from ddp_corpus.deps import Actor, current_actor
from ddp_corpus.errors import APIError

router = APIRouter()


class SourceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resource_id: str = Field(min_length=1, max_length=32)
    source_version_id: str = Field(min_length=1, max_length=32)


class BuildIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=255)
    sources: list[SourceIn] = Field(min_length=1, max_length=50)
    max_pages: int = Field(default=4, ge=1, le=12)
    max_evidence: int = Field(default=50, ge=1, le=200)
    max_output_tokens: int = Field(default=4096, ge=256, le=16384)
    max_input_chars: int = Field(default=50000, ge=1000, le=200000)


class RebuildIn(BuildIn):
    base_revision_id: str = Field(min_length=1, max_length=32)


class Paragraph(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=10000)


class EditIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_revision_id: str = Field(min_length=1, max_length=32)
    paragraphs: list[Paragraph] = Field(max_length=100)


class PublishIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_revision_id: str = Field(min_length=1, max_length=32)


def _write_actor(actor):
    actor.require(actor.can_upload and actor.principal_id is not None, "编写 Wiki")


async def _write(session, operation):
    try:
        return await operation
    except IntegrityError:
        await session.rollback()
        wiki.fail(409, "revision_conflict", "Concurrent Wiki write; retry with the same key")
    except httpx.HTTPError:
        await session.rollback()
        wiki.fail(502, "wiki_generation_failed", "Wiki model request failed")
    except APIError:
        await session.rollback()
        raise
    except Exception:
        await session.rollback()
        raise


@router.post("/wikis", status_code=201)
async def create(body: BuildIn, request: Request,
                 idempotency_key: str = Header(min_length=1, max_length=128),
                 actor: Actor = Depends(current_actor),
                 session: AsyncSession = Depends(get_session)):
    _write_actor(actor)
    return await _write(session, wiki.build(session, actor, request.app.state.http,
                                            body.model_dump(), idempotency_key))


@router.get("/wikis")
async def list_(actor: Actor = Depends(current_actor),
                session: AsyncSession = Depends(get_session)):
    return await wiki.list_wikis(session, actor)


@router.get("/wikis/{wiki_id}")
async def read(wiki_id: str, actor: Actor = Depends(current_actor),
               session: AsyncSession = Depends(get_session)):
    return await wiki.revision_out(session, actor, await wiki.get_wiki(session, actor, wiki_id))


@router.get("/wikis/{wiki_id}/revisions/{revision_id}")
async def read_revision(wiki_id: str, revision_id: str, actor: Actor = Depends(current_actor),
                        session: AsyncSession = Depends(get_session)):
    return await wiki.revision_out(session, actor, await wiki.get_wiki(session, actor, wiki_id),
                                   revision_id)


@router.post("/wikis/{wiki_id}/revisions", status_code=201)
async def rebuild(wiki_id: str, body: RebuildIn, request: Request,
                  idempotency_key: str = Header(min_length=1, max_length=128),
                  actor: Actor = Depends(current_actor),
                  session: AsyncSession = Depends(get_session)):
    _write_actor(actor)
    return await _write(session, wiki.build(session, actor, request.app.state.http,
        body.model_dump(), idempotency_key, wiki_id=wiki_id))


@router.patch("/wikis/{wiki_id}/pages/{page_key}", status_code=201)
async def edit(wiki_id: str, page_key: str, body: EditIn,
               idempotency_key: str = Header(min_length=1, max_length=128),
               actor: Actor = Depends(current_actor),
               session: AsyncSession = Depends(get_session)):
    _write_actor(actor)
    return await _write(session, wiki.edit_page(session, actor, wiki_id, page_key,
                                               body.model_dump(), idempotency_key))


@router.post("/wikis/{wiki_id}/publish")
async def publish(wiki_id: str, body: PublishIn, actor: Actor = Depends(current_actor),
                  session: AsyncSession = Depends(get_session)):
    _write_actor(actor)
    return await _write(session, wiki.publish(session, actor, wiki_id, body.base_revision_id))
