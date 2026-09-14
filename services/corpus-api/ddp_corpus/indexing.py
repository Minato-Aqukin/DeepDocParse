"""Index one fixed ParseJob; shared Document fields are only a compatibility mirror."""
import asyncio
import json
from contextlib import suppress
from datetime import timedelta

import httpx
from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ddp_core.anchor import digest_of
from ddp_core.compilation import code_detection_of, fingerprint, source_anchor
from ddp_corpus.compilation import CompileOutput, compile_document
from ddp_corpus.config import settings
from ddp_corpus.usage import record_usage
from ddp_corpus.models import Chunk, Document, Evidence, ParseJob, Resource, ResourceVersion, new_id, utcnow
from ddp_corpus.storage import Storage
from ddp_corpus.upstream import embed_texts

_STATE_FIELDS = ("index_status", "index_error", "index_generation", "index_lease_until",
                 "compile_status", "compile_degraded", "compile_fingerprint", "layout_version", "code_detection")


class IndexSuperseded(RuntimeError):
    pass


class IndexSourceUnavailable(RuntimeError):
    pass


def _lease_deadline():
    return utcnow() + timedelta(seconds=settings.index_lease_seconds)


async def _resolve_job_id(session, document_id, job_id=None):
    if job_id is not None:
        return job_id
    return await session.scalar(select(Document.current_job_id).where(Document.id == document_id))


async def _lock_document(session, document_id):
    return await session.scalar(select(Document).where(Document.id == document_id)
        .with_for_update().execution_options(populate_existing=True))


async def _mirror_job(session, job):
    await session.execute(update(Document).where(Document.id == job.document_id,
        Document.current_job_id == job.id).values(
            **{name: getattr(job, name) for name in _STATE_FIELDS}, updated_at=utcnow())
        .execution_options(synchronize_session=False))


async def mark_index_pending(session: AsyncSession, job_id: str) -> int | None:
    """Invalidate only this job's workers and schedule a fresh generation; caller commits."""
    job = await session.get(ParseJob, job_id)
    if job is None:
        return None
    document = await _lock_document(session, job.document_id)
    if document is None or document.deleted_at is not None:
        return None
    result = await session.execute(update(ParseJob).where(ParseJob.id == job_id).values(
        index_status="pending", index_error=None, index_generation=ParseJob.index_generation + 1,
        index_lease_until=None, compile_status="pending", compile_degraded=[], updated_at=utcnow())
        .returning(ParseJob.index_generation).execution_options(synchronize_session=False))
    generation = result.scalar_one_or_none()
    await session.refresh(job)
    await _mirror_job(session, job)
    return generation


async def claim_for_indexing(session: AsyncSession, document_id: str, *, job_id=None) -> int | None:
    job_id = await _resolve_job_id(session, document_id, job_id)
    document = await _lock_document(session, document_id)
    if document is None or document.deleted_at is not None or not job_id:
        await session.rollback()
        return None
    now = utcnow()
    result = await session.execute(update(ParseJob).where(
        ParseJob.id == job_id, ParseJob.document_id == document_id,
        or_(ParseJob.index_status == "pending", and_(ParseJob.index_status == "indexing",
            or_(ParseJob.index_lease_until.is_(None), ParseJob.index_lease_until < now))))
        .values(index_status="indexing", index_error=None, compile_status="compiling",
                compile_degraded=[], index_generation=ParseJob.index_generation + 1,
                index_lease_until=_lease_deadline(), updated_at=now)
        .returning(ParseJob.index_generation).execution_options(synchronize_session=False))
    generation = result.scalar_one_or_none()
    if generation is not None:
        job = await session.get(ParseJob, job_id, populate_existing=True)
        await _mirror_job(session, job)
    await session.commit()
    return generation


async def _authorized_job(session, document_id, job_id, generation):
    document = await session.get(Document, document_id, populate_existing=True)
    job = await session.get(ParseJob, job_id, populate_existing=True)
    if (job is None or job.document_id != document_id or job.index_status != "indexing"
            or job.index_generation != generation):
        raise IndexSuperseded("index generation changed")
    if document is None or document.deleted_at is not None or not job.result_prefix:
        raise IndexSourceUnavailable("parse source is unavailable")
    if job.resource_id:
        resource = await session.get(Resource, job.resource_id, populate_existing=True)
        binding = await session.scalar(select(ResourceVersion.id).where(
            ResourceVersion.resource_id == job.resource_id, ResourceVersion.document_id == document_id,
            ResourceVersion.parse_job_id == job_id, ResourceVersion.deleted_at.is_(None)).limit(1))
        if (resource is None or resource.deleted_at is not None or not binding
                or resource.publication not in ("private", "draft", "published")):
            raise IndexSourceUnavailable("resource was withdrawn, deleted or unbound")
        # Copied assets retain their source ancestry; a withdrawn ancestor stops new processing.
        seen = {resource.id}
        parent_id = resource.copied_from
        while parent_id:
            if parent_id in seen:
                raise IndexSourceUnavailable("resource ancestry cycle")
            seen.add(parent_id)
            parent = await session.get(Resource, parent_id, populate_existing=True)
            if parent is None or parent.deleted_at is not None or parent.publication != "published":
                raise IndexSourceUnavailable("resource origin is no longer public")
            parent_id = parent.copied_from
        actor_id, organization_id = job.initiated_by or resource.owner_id, resource.organization_id
    else:
        if await session.scalar(select(ResourceVersion.id).where(
                ResourceVersion.document_id == document_id).limit(1)):
            raise IndexSourceUnavailable("historical parse has no logical resource binding")
        actor_id, organization_id = job.initiated_by or document.uploaded_by, document.organization_id
    return document, job, actor_id, organization_id


class _AuthorizedHTTP:
    """Guard each actual model/embedding request without duplicating compilation."""
    def __init__(self, http, check):
        self.http, self.check = http, check
        self.model_requests = 0
        self.embedding_requests = 0
        self.lock = asyncio.Lock()

    def __getattr__(self, name):
        return getattr(self.http, name)

    async def _before(self):
        # Compile may schedule visual atoms concurrently; AsyncSession cannot execute
        # multiple policy reads simultaneously, so only the short guard is serialized.
        async with self.lock:
            await self.check()

    async def send(self, *args, **kwargs):
        await self._before()
        self.model_requests += 1
        return await self.http.send(*args, **kwargs)

    async def post(self, *args, **kwargs):
        await self._before()
        self.embedding_requests += 1
        return await self.http.post(*args, **kwargs)


async def index_document(session: AsyncSession, storage: Storage, http: httpx.AsyncClient,
                         document_id: str, *, job_id: str | None = None) -> int:
    job_id = await _resolve_job_id(session, document_id, job_id)
    generation = await claim_for_indexing(session, document_id, job_id=job_id)
    if generation is None:
        return 0
    heartbeat = asyncio.create_task(_heartbeat_lease(session.bind, job_id, generation))
    try:
        return await _index_claimed(session, storage, http, document_id=document_id,
                                    job_id=job_id, generation=generation)
    finally:
        heartbeat.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat


async def _index_claimed(session, storage, http, *, document_id, generation, job_id=None):
    job_id = await _resolve_job_id(session, document_id, job_id)
    try:
        document, job, actor_id, organization_id = await _authorized_job(
            session, document_id, job_id, generation)
    except IndexSuperseded:
        await session.rollback()
        return 0
    except IndexSourceUnavailable as exc:
        await _fail_if_current(session, document_id, job_id, generation, str(exc))
        return 0

    async def check():
        await _authorized_job(session, document_id, job_id, generation)
    guarded_http = _AuthorizedHTTP(http, check)
    try:
        raw = await storage.get(f"{job.result_prefix}layout.json")
        layout = json.loads(raw.decode())
        await check()
        compiled = await compile_document(storage=storage, http=guarded_http, document=document,
                                          job=job, layout=layout)
        chunks = compiled.chunks
    except IndexSuperseded:
        await session.rollback()
        return 0
    except Exception as exc:
        await _fail_if_current(session, document_id, job_id, generation,
            f"编译版面失败：{type(exc).__name__}: {exc}", compile_failed=True)
        return 0
    if guarded_http.model_requests:
        await record_usage(session, actor_id=actor_id, organization_id=organization_id,
                           parse_job_id=job_id, kind="compile_vision", requests=guarded_http.model_requests)
        await session.commit()
    if not chunks or not any(c.get("search_text") for c in chunks):
        await _fail_if_current(session, document_id, job_id, generation,
                               "文档没有可检索的文本块（可能是纯图片扫描件）")
        return 0
    async def account_embeddings():
        if guarded_http.embedding_requests:
            await record_usage(session, actor_id=actor_id, organization_id=organization_id,
                parse_job_id=job_id, kind="embed", requests=guarded_http.embedding_requests)
            await session.commit()
    try:
        vectors = await _embed_sparse(guarded_http, [c.get("search_text") or "" for c in chunks])
    except IndexSuperseded:
        await account_embeddings()
        await session.rollback()
        return 0
    except Exception as exc:
        await account_embeddings()
        await _fail_if_current(session, document_id, job_id, generation,
            f"向量化失败：{type(exc).__name__}: {exc}", layout=layout, compiled=compiled)
        return 0
    await account_embeddings()
    # Global lock order is Document -> ParseJob, matching archive/reindex transactions.
    await _lock_document(session, document_id)
    await session.execute(select(ParseJob.id).where(ParseJob.id == job_id).with_for_update())
    try:
        document, job, _, _ = await _authorized_job(session, document_id, job_id, generation)
    except IndexSuperseded:
        await session.rollback()
        return 0
    except IndexSourceUnavailable as exc:
        await _fail_if_current(session, document_id, job_id, generation, str(exc))
        return 0
    await session.execute(delete(Chunk).where(Chunk.parse_job_id == job_id))
    rows = await _materialize_evidence(session, document, job, compiled)
    session.add_all([Chunk(
        document_id=document_id, parse_job_id=job_id, seq=c["seq"],
        page_idx=c["page_idx"], bbox=c["bbox"], page_size=c["page_size"],
        text=c["text"], search_text=c.get("search_text") or "", derived_text=c.get("derived_text"),
        char_len=c["char_len"], block_type=c.get("block_type", "text"), table_html=c.get("table_html"),
        text_tokenized=c.get("text_tokenized", ""), provider=c.get("provider") or {},
        provider_fingerprint=c.get("provider_fingerprint") or "", evidence_id=rows[c["seq"]][0].id,
        derived_evidence_id=rows[c["seq"]][1].id if rows[c["seq"]][1] else None, embedding=vec,
    ) for c, vec in zip(chunks, vectors, strict=True)])
    _record_compile_state(job, layout, compiled)
    job.index_status, job.index_error, job.index_lease_until = "ready", None, None
    job.updated_at = utcnow()
    await _mirror_job(session, job)
    await session.commit()
    return len(chunks)


async def _renew_lease_once(factory, job_id: str, generation: int) -> bool:
    async with factory() as heartbeat:
        result = await heartbeat.execute(update(ParseJob).where(ParseJob.id == job_id,
            ParseJob.index_status == "indexing", ParseJob.index_generation == generation)
            .values(index_lease_until=_lease_deadline(), updated_at=utcnow()))
        await heartbeat.commit()
        return result.rowcount > 0


async def _heartbeat_lease(bind, job_id: str, generation: int) -> None:
    factory = async_sessionmaker(bind, expire_on_commit=False)
    while True:
        await asyncio.sleep(settings.index_heartbeat_seconds)
        if not await asyncio.shield(_renew_lease_once(factory, job_id, generation)):
            return


async def _embed_all(http, texts: list[str]) -> list[list[float]]:
    vectors = []
    for start in range(0, len(texts), settings.embedding_batch_size):
        vectors.extend(await embed_texts(http, texts[start:start + settings.embedding_batch_size]))
    return vectors


async def _embed_sparse(http, texts: list[str]) -> list[list[float] | None]:
    positions = [i for i, value in enumerate(texts) if value.strip()]
    embedded = await _embed_all(http, [texts[i] for i in positions]) if positions else []
    out: list[list[float] | None] = [None] * len(texts)
    for index, vector in zip(positions, embedded, strict=True):
        out[index] = vector
    return out


def _anchor_key(chunk: dict) -> str:
    import hashlib
    payload = json.dumps({"text": chunk["text"], "page": chunk["page_idx"],
                          "bbox": chunk.get("bbox")}, sort_keys=True,
                         ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()[:16]


async def _materialize_evidence(session: AsyncSession, document: Document, job: ParseJob,
                                compiled: CompileOutput
                                ) -> dict[int, tuple[Evidence, Evidence | None]]:
    """源/派生 Evidence 幂等落库；内容变化时保留旧行供历史 citation 判失效。"""
    existing = (await session.execute(
        select(Evidence).where(Evidence.parse_job_id == job.id)
    )).scalars().all()
    source_by_anchor = {
        source_anchor(seq=e.seq, content_digest=e.content_digest,
                      page_idx=e.page_idx, bbox=e.bbox): e
        for e in existing if e.derived_from is None
    }
    derived_by_anchor = {
        (e.seq, e.content_digest, e.derived_from): e
        for e in existing if e.derived_from is not None
    }
    rows: dict[int, tuple[Evidence, Evidence | None]] = {}
    fp = fingerprint(compiled.provider)

    for chunk in compiled.chunks:
        seq = chunk["seq"]
        digest = digest_of(chunk["text"])
        key = source_anchor(seq=seq, content_digest=digest,
                            page_idx=chunk["page_idx"], bbox=chunk.get("bbox"))
        source = source_by_anchor.get(key)
        if source is None:
            source = Evidence(
                id=new_id(), document_id=document.id, doc_version=job.document_version,
                parse_job_id=job.id, seq=seq,
                atom_key=f"source:{seq}:{_anchor_key(chunk)}",
                page_idx=chunk["page_idx"], bbox=chunk.get("bbox"),
                page_size=chunk.get("page_size"), kind=chunk["block_type"],
                crop_key=compiled.crop_keys.get(seq), content_digest=digest,
                content=chunk["text"], provider=compiled.provider,
                provider_fingerprint=fp,
            )
            session.add(source)
            await session.flush()
            source_by_anchor[key] = source
        elif source.crop_key is None and compiled.crop_keys.get(seq):
            source.crop_key = compiled.crop_keys[seq]

        derived = None
        if chunk.get("derived_text"):
            derived_digest = digest_of(chunk["derived_text"])
            dkey = (seq, derived_digest, source.id)
            derived = derived_by_anchor.get(dkey)
            if derived is None:
                derived_provider = {**compiled.provider, "content_role": "generated"}
                derived = Evidence(
                    id=new_id(), document_id=document.id, doc_version=job.document_version,
                    parse_job_id=job.id, seq=seq,
                    atom_key=f"vision:{seq}:{derived_digest[:16]}",
                    page_idx=chunk["page_idx"], bbox=chunk.get("bbox"),
                    page_size=chunk.get("page_size"), kind=chunk["block_type"],
                    crop_key=compiled.crop_keys.get(seq), content_digest=derived_digest,
                    content=chunk["derived_text"], provider=derived_provider,
                    provider_fingerprint=fingerprint(derived_provider), derived_from=source.id,
                )
                session.add(derived)
                await session.flush()
                derived_by_anchor[dkey] = derived
        rows[seq] = (source, derived)
    return rows


def _record_compile_state(document: ParseJob, layout: dict, compiled: CompileOutput) -> None:
    document.compile_status = "partial" if compiled.degraded else "ready"
    document.compile_degraded = compiled.degraded
    document.compile_fingerprint = fingerprint(compiled.provider)
    document.layout_version = str(layout.get("layout_version") or "")
    document.code_detection = code_detection_of(layout)


def _batch_count(total: int) -> int:
    size = settings.embedding_batch_size
    return (total + size - 1) // size


async def _fail_if_current(session, document_id, job_id, generation, reason, *,
                           compile_failed=False, layout=None, compiled=None):
    await _lock_document(session, document_id)
    values = {"index_status": "failed", "index_error": reason, "index_lease_until": None,
              "updated_at": utcnow()}
    if compile_failed:
        values.update(compile_status="failed", compile_degraded=["compile_failed"])
    elif layout is not None and compiled is not None:
        values.update(compile_status="partial" if compiled.degraded else "ready",
            compile_degraded=compiled.degraded, compile_fingerprint=fingerprint(compiled.provider),
            layout_version=str(layout.get("layout_version") or ""), code_detection=code_detection_of(layout))
    result = await session.execute(update(ParseJob).where(ParseJob.id == job_id,
        ParseJob.document_id == document_id, ParseJob.index_generation == generation,
        ParseJob.index_status == "indexing").values(**values)
        .execution_options(synchronize_session=False))
    if result.rowcount:
        job = await session.get(ParseJob, job_id, populate_existing=True)
        await _mirror_job(session, job)
    await session.commit()
