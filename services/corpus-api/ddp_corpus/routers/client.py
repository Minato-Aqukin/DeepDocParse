"""Fixed read/reconciliation routes for the authenticated center Provider."""
from dataclasses import replace
from datetime import datetime, timezone
import base64
import hashlib
import json
import math
import re
from fastapi import APIRouter, Depends, Header, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ddp_corpus import client_projection as projection
from ddp_corpus.capabilities import collect_capability_profiles
from ddp_corpus.config import settings
from ddp_corpus.client_models import ClientPage, ClientReceipt, ClientSnapshot, ClientView
from ddp_corpus.db import get_session
from ddp_corpus.deps import Actor, current_actor, get_storage, require_service_actor
from ddp_corpus.models import Document, Resource, ResourceVersion, Wiki, WikiPage, WikiRevision, as_aware
from ddp_corpus.policy import resource_condition
from ddp_corpus.routers import mcp_tools

router = APIRouter()
CAPABILITIES = ["client.snapshot", "client.events", "client.receipt", "client.query", "client.windows", "client.assets"]


async def client_scope(actor: Actor = Depends(current_actor),
                       x_ddp_client_scope: str = Header(default="")):
    return projection.scope_for(actor, x_ddp_client_scope)


def response(value):
    safe = jsonable_encoder(value)
    if len(projection.encoded(safe)) > 3 * 1024 * 1024:
        raise projection.error("projection_too_large", 507)
    return JSONResponse(safe, headers={"Cache-Control": "no-store"})


async def capabilities(request):
    profiles, status = await collect_capability_profiles(request.app.state.http)
    # Observation/lease timestamps are not source events. Readiness/config/model changes are.
    return {"capabilities": [{k: v for k, v in profile.items() if k not in ("observed_at", "valid_until")}
                              for profile in profiles], "capability_status": status,
            "model_configuration": {"parse_engine":settings.default_parse_engine,
                "chat_model":settings.chat_model, "embedding_model":settings.embedding_model,
                "rerank_model":settings.rerank_model, "rerank_enabled":settings.rerank_enabled,
                "compile_vision_enabled":settings.compile_vision_enabled}}


@router.get("/internal/client/protocol")
async def protocol(_: Actor = Depends(require_service_actor), session: AsyncSession = Depends(get_session)):
    # Only advertise the interface when its persisted stores really exist.
    for model in (ClientView, ClientSnapshot, ClientPage, ClientReceipt):
        await session.execute(select(model).limit(0))
    return {"protocol_version": "ddp-client/1", "capabilities": CAPABILITIES}


@router.get("/api/v1/client/snapshot")
async def snapshot(request: Request, actor: Actor = Depends(current_actor),
                   scope: str = Depends(client_scope), session: AsyncSession = Depends(get_session)):
    return response(await projection.observe(session, actor, scope, await capabilities(request)))


@router.get("/api/v1/client/events")
async def events(request: Request, after: str = "", actor: Actor = Depends(current_actor),
                 scope: str = Depends(client_scope), session: AsyncSession = Depends(get_session)):
    if not after or len(after) > 128:
        raise projection.error()
    return response(await projection.observe(session, actor, scope, await capabilities(request), after=after))


@router.get("/api/v1/client/receipts/{operation_key}")
async def receipt(operation_key: str, actor: Actor = Depends(current_actor),
                  scope: str = Depends(client_scope), session: AsyncSession = Depends(get_session)):
    if not operation_key or len(operation_key) > 128:
        raise projection.error("receipt_not_found", 404)
    return response(await projection.receipt(session, actor, operation_key))


async def _asset_version(session, actor, version_id):
    version = await session.scalar(
        select(ResourceVersion).join(Resource).join(
            Document, Document.id == ResourceVersion.document_id
        ).where(
            ResourceVersion.id == version_id,
            ResourceVersion.deleted_at.is_(None),
            Document.deleted_at.is_(None),
            resource_condition(actor),
        )
    )
    if version is None:
        raise projection.error("not_found", 404)
    return version


def _asset_identity(result, request, actor):
    result.headers["Cache-Control"] = "private, no-store"
    result.headers["X-DDP-Authority-Node"] = request.headers.get("X-DDP-Authority-Node", "")
    result.headers["X-DDP-Actor-Subject"] = actor.principal_id
    return result


@router.get("/api/v1/client/versions/{version_id}/source")
async def source_asset(
    version_id: str, request: Request, actor: Actor = Depends(current_actor),
    scope: str = Depends(client_scope), session: AsyncSession = Depends(get_session),
    storage=Depends(get_storage),
):
    from ddp_corpus.bundle_source import source_response

    version = await _asset_version(session, actor, version_id)
    result = await source_response(
        version.resource_id, version.id, actor=actor, session=session,
        storage=storage, http=request.app.state.http,
    )
    return _asset_identity(result, request, actor)


@router.get("/api/v1/client/versions/{version_id}/bundle")
async def bundle_asset(
    version_id: str, request: Request, actor: Actor = Depends(current_actor),
    scope: str = Depends(client_scope), session: AsyncSession = Depends(get_session),
    storage=Depends(get_storage),
):
    from ddp_corpus.routers.bundles import export_bundle

    version = await _asset_version(session, actor, version_id)
    result = await export_bundle(
        version.resource_id, version.id, actor=actor, session=session, storage=storage,
        include_wiki=False, include_vectors=False,
    )
    return _asset_identity(result, request, actor)


class Query(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(max_length=64)
    payload: dict


class PageQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    snapshot_id: str = Field(min_length=1, max_length=128)
    cursor: str = Field(min_length=1, max_length=128)


class SearchQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=4000)
    limit: int = Field(default=20, ge=1, le=50)
    resource_id: str | None = Field(default=None, max_length=128)
    version_id: str | None = Field(default=None, max_length=128)
    version_ids: list[str] | None = Field(default=None, max_length=1000)


class EvidenceQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evidence_id: str = Field(min_length=1, max_length=512)
    resource_id: str | None = Field(default=None, max_length=128)
    version_id: str | None = Field(default=None, max_length=128)


class WikiWindowQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cursor: str | None = Field(default=None, min_length=1, max_length=1024)
    limit: int = Field(default=50, ge=1, le=100, strict=True)


class WikiRevisionsQuery(WikiWindowQuery):
    wiki_id: str = Field(min_length=1, max_length=32)


class WikiGetQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    wiki_id: str = Field(min_length=1, max_length=32)
    revision_id: str | None = Field(default=None, min_length=1, max_length=32)


async def client_wiki_window(session, actor, scope, args, *, wiki_id=None):
    from ddp_corpus import wiki

    model = WikiRevision if wiki_id else Wiki
    list_scope = "revisions:" + wiki_id if wiki_id else "wikis"
    if wiki_id:
        selected_wiki = await wiki.get_wiki(session, actor, wiki_id)
        predicates = [WikiRevision.wiki_id == wiki_id]
        if not wiki.is_owner(selected_wiki, actor):
            # A public revision's source revocation also revokes its history metadata.
            await wiki.revision_out(session, actor, selected_wiki)
            predicates.append(WikiRevision.id == selected_wiki.published_revision_id)
    else:
        predicates = [Wiki.organization_id == actor.organization_id,
                      Wiki.owner_id == actor.principal_id,
                      Wiki.current_revision_id.is_not(None)]

    anchor = after = None
    if args.cursor:
        try:
            decoded = json.loads(base64.urlsafe_b64decode(
                args.cursor.encode() + b"=" * (-len(args.cursor) % 4)))
            if decoded["workspace"] != scope or decoded["scope"] != list_scope:
                raise ValueError()
            anchor, after = decoded["anchor"], decoded["after"]
            for pair in (anchor, after):
                if (not isinstance(pair, list) or len(pair) != 2
                        or type(pair[0]) not in (int, float) or not math.isfinite(pair[0])
                        or not isinstance(pair[1], str) or not re.fullmatch(r"[a-f0-9]{32}", pair[1])):
                    raise ValueError()
                datetime.fromtimestamp(pair[0], timezone.utc)
            if after > anchor:
                raise ValueError()
        except (ValueError, KeyError, TypeError, UnicodeError, OverflowError, OSError):
            raise projection.error("cursor_expired", 409) from None
    if anchor is None:
        top = (await session.execute(select(model.created_at, model.id).where(*predicates)
            .order_by(model.created_at.desc(), model.id.desc()).limit(1))).first()
        if top is None:
            return {"items": [], "visible_total": 0, "has_more": False, "next_cursor": None}
        anchor = [as_aware(top.created_at).timestamp(), top.id]

    def boundary(pair, *, inclusive):
        timestamp = datetime.fromtimestamp(pair[0], timezone.utc)
        return or_(model.created_at < timestamp, and_(model.created_at == timestamp,
                   model.id <= pair[1] if inclusive else model.id < pair[1]))

    predicates.append(boundary(anchor, inclusive=True))
    total = await session.scalar(select(func.count()).select_from(model).where(*predicates))
    if after is not None:
        predicates.append(boundary(after, inclusive=False))
    columns = [model.id, model.created_at]
    if wiki_id:
        columns += [WikiRevision.wiki_id, WikiRevision.base_revision_id, WikiRevision.kind]
    else:
        columns += [Wiki.title, Wiki.current_revision_id, Wiki.published_revision_id]
    entries = (await session.execute(select(*columns).where(*predicates)
        .order_by(model.created_at.desc(), model.id.desc()).limit(args.limit + 1))).all()
    has_more, selected, items = len(entries) > args.limit, entries[:args.limit], []
    for entry in selected:
        if wiki_id:
            items.append(dict(entry._mapping))
            continue
        metadata = (await session.execute(select(
            WikiRevision.id, WikiRevision.wiki_id, WikiRevision.title,
            WikiRevision.base_revision_id, WikiRevision.kind, WikiRevision.created_at,
            func.coalesce(func.json_array_length(WikiRevision.relations), 0).label("relation_count"),
            select(func.count(WikiPage.id)).where(WikiPage.revision_id == WikiRevision.id)
                .correlate(WikiRevision).scalar_subquery().label("page_count"),
        ).where(WikiRevision.id == entry.current_revision_id,
                WikiRevision.wiki_id == entry.id))).one()
        items.append({"wiki": {"id": entry.id, "title": metadata.title,
                      "current_revision_id": entry.current_revision_id,
                      "published_revision_id": entry.published_revision_id},
                      "revision": {**dict(metadata._mapping), "semantic_review": "needs_review",
                                   "source_type": "generated"}})
    next_cursor = None
    if has_more:
        last = selected[-1]
        next_cursor = base64.urlsafe_b64encode(projection.encoded({
            "workspace": scope, "scope": list_scope, "anchor": anchor,
            "after": [as_aware(last.created_at).timestamp(), last.id],
        })).decode().rstrip("=")
    return {"items": items, "visible_total": total, "has_more": has_more, "next_cursor": next_cursor}


@router.post("/api/v1/client/query")
async def query(body: Query, request: Request, actor: Actor = Depends(current_actor),
                scope: str = Depends(client_scope), session: AsyncSession = Depends(get_session)):
    from pydantic import ValidationError
    try:
        if body.name in ("resource.page", "task.page"):
            args = PageQuery.model_validate(body.payload)
            kind = "resources" if body.name == "resource.page" else "tasks"
            result = await projection.page(session, actor, scope, args.snapshot_id, args.cursor, kind)
        elif body.name == "corpus.search":
            args = SearchQuery.model_validate(body.payload)
            chosen = projection.bindings_statement(actor)
            if args.version_ids is not None:
                if args.resource_id is not None or args.version_id is not None or any(not v or len(v)>128 for v in args.version_ids):
                    raise projection.error("invalid_query", 400)
                allowed = set((await session.execute(chosen.where(
                    ResourceVersion.id.in_(args.version_ids)))).all())
                if {v[1] for v in allowed} != set(args.version_ids):
                    raise projection.error("version_not_found", 404)
                selected = args.version_ids
            else:
                if args.resource_id:
                    chosen = chosen.where(Resource.id == args.resource_id)
                if args.version_id:
                    chosen = chosen.where(ResourceVersion.id == args.version_id)
                selected = [row[1] for row in (await session.execute(chosen)).all()]
                if not selected and (args.resource_id or args.version_id):
                    raise projection.error("version_not_found", 404)
            actor = replace(actor, resource_id=args.resource_id, version_id=args.version_id)
            result = await mcp_tools._search(request, session, actor, query=args.query, limit=args.limit,
                                            version_ids=selected)
            # Recheck selected version metadata as well as hit content after model/index awaits.
            # Otherwise a withdrawn version could survive in the response's scope inventory.
            current_ids = {row[1] for row in (await session.execute(projection.bindings_statement(actor)
                .where(ResourceVersion.id.in_(selected)))).all()}
            if (args.version_ids is not None or args.resource_id or args.version_id) and current_ids != set(selected):
                raise projection.error("version_not_found", 404)
            result["results"] = [item for item in result["results"] if item["source_version_id"] in current_ids]
            for item in result["results"]:
                item["version_id"] = item["source_version_id"]
                item["text"] = item["content"]
                item["parse_job_id"] = item["parse_revision"]
                item["copies"] = [copy for copy in item["copies"] if copy["source_version_id"] in current_ids]
            result["hits"] = result.pop("results")
            result["scope"]["source_version_ids"] = sorted(current_ids)
            result["scope"]["snapshot_complete"] = True
        elif body.name == "wiki.list":
            args = WikiWindowQuery.model_validate(body.payload)
            result = await client_wiki_window(session, actor, scope, args)
        elif body.name == "wiki.revisions":
            args = WikiRevisionsQuery.model_validate(body.payload)
            result = await client_wiki_window(session, actor, scope, args, wiki_id=args.wiki_id)
        elif body.name == "wiki.get":
            from ddp_corpus import wiki
            args = WikiGetQuery.model_validate(body.payload)
            selected_wiki = await wiki.get_wiki(session, actor, args.wiki_id)
            result = await wiki.revision_out(session, actor, selected_wiki, args.revision_id)
        elif body.name == "evidence.get":
            args = EvidenceQuery.model_validate(body.payload)
            actor = replace(actor, resource_id=args.resource_id, version_id=args.version_id)
            result = await mcp_tools.get_evidence(args.evidence_id, request, actor, session)
            result = await client_evidence(session, actor, request, result)
        else:
            raise projection.error("unsupported_operation", 400)
    except ValidationError:
        raise projection.error("invalid_query", 400) from None
    return response(result)


async def client_evidence(session, actor, request, result):
    payload = result["evidence"]
    version_id = payload.get("source_version_id")
    binding = (await session.execute(select(ResourceVersion, Resource, Document).join(Resource).join(
        Document, Document.id == ResourceVersion.document_id).where(
        ResourceVersion.id == version_id, ResourceVersion.deleted_at.is_(None),
        Document.deleted_at.is_(None), ResourceVersion.parse_job_id == payload["parse_revision"],
        resource_condition(actor)))).first()
    if binding is None:
        raise projection.error("not_found", 404)
    version, resource, document = binding
    node = request.headers.get("X-DDP-Authority-Node", "")
    if document.origin != "web" or version.bundle_prefix or not re.fullmatch(r"node-[0-9a-f]{48}", node) or not re.fullmatch(r"[0-9a-f]{64}", version.source_digest):
        raise projection.error("evidence_provenance_unavailable", 409)
    page = payload.get("page_size")
    size = {"width":page[0], "height":page[1]} if isinstance(page, list) and len(page)==2 else page
    envelope = {"schema":"ddp-evidence/1#FederatedEvidence", "evidence_id":payload["evidence_id"],
        "origin_node_id":node, "authority_node_id":node, "resource_id":resource.id,
        "source_version_id":version.id, "source_digest":"sha256:"+version.source_digest,
        "parse_revision":payload["parse_revision"],
        "excerpt_digest":"sha256:"+hashlib.sha256(payload["content"].encode()).hexdigest(),
        "locator":{"kind":"page_block", "physical_page_index":payload["page_idx"], "seq":payload["seq"],
                   "bbox":payload["bbox"], "page_size":size,
                   **({"printed_page_label":payload["printed_page_label"]}
                      if payload.get("printed_page_label") is not None else {})},
        "source_type":payload["source_type"], "derived_from":payload.get("derived_from"),
        "uploader_ref":resource.uploaded_by, "retrieval_receipt_ref":None,
        "policy_revision":resource.publication+":"+as_aware(resource.updated_at).isoformat(),
        "block_type":payload["kind"]}
    return {"id":payload["evidence_id"], "version_id":version.id, "excerpt":payload["content"],
            "evidence":envelope, "crop":result.get("crop")}
