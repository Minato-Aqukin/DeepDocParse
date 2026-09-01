r"""向量索引管线：归档好的 layout.json -> 分块 -> 向量化 -> 落库。

状态机（documents.index_status）：
    none -> pending -> indexing -> ready
                               \-> failed（index_error 落库，UI 可见，可手动 reindex）

两条硬规矩：
1. **claim 幂等**：多副本/重复投递只领取显式排队的 pending，
   或接管 lease 已过期的 indexing；failed 必须由用户显式 reindex 后才会回到 pending，
   抢不到直接返回（沿用 M5 归档 claim 的套路）。
2. **失败不自动重试**：重试要重跑全量 embedding，成本高且大概率同样失败。
   落 failed + 原因，等用户/运维显式 reindex —— 静默失败是这个项目吃过大亏的地方。
"""
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
from ddp_corpus.metering import record_usage
from ddp_corpus.models import Chunk, Document, Evidence, ParseJob, new_id, utcnow
from ddp_corpus.storage import Storage
from ddp_corpus.upstream import embed_texts


def _lease_deadline():
    return utcnow() + timedelta(seconds=settings.index_lease_seconds)


async def claim_for_indexing(session: AsyncSession, document_id: str) -> int | None:
    """抢占索引并返回 fencing generation；过期 lease 可由新 worker 接管。"""
    now = utcnow()
    result = await session.execute(
        update(Document)
        .where(Document.id == document_id, Document.deleted_at.is_(None),
               or_(Document.index_status == "pending",
                   and_(Document.index_status == "indexing",
                        or_(Document.index_lease_until.is_(None),
                            Document.index_lease_until < now))))
        .values(index_status="indexing", index_error=None, compile_status="compiling",
                compile_degraded=[], index_generation=Document.index_generation + 1,
                index_lease_until=_lease_deadline(), updated_at=now)
        .returning(Document.index_generation)
    )
    generation = result.scalar_one_or_none()
    await session.commit()
    return generation


async def index_document(session: AsyncSession, storage: Storage, http: httpx.AsyncClient,
                         document_id: str) -> int:
    """返回写入的 chunk 数；被别人 claim 或无可索引内容时返回 0。"""
    generation = await claim_for_indexing(session, document_id)
    if generation is None:
        return 0

    heartbeat = asyncio.create_task(
        _heartbeat_lease(session.bind, document_id, generation))
    try:
        return await _index_claimed(
            session, storage, http, document_id=document_id, generation=generation)
    finally:
        heartbeat.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat


async def _index_claimed(session: AsyncSession, storage: Storage, http: httpx.AsyncClient,
                         *, document_id: str, generation: int) -> int:

    document = await session.get(Document, document_id)
    if document is None:
        return 0
    job = await session.get(ParseJob, document.current_job_id) if document.current_job_id else None
    if job is None or not job.result_prefix:
        await _fail_if_current(session, document.id, document.current_job_id, generation,
                               "没有可用的解析结果")
        return 0

    try:
        raw = await storage.get(f"{job.result_prefix}layout.json")
        layout = json.loads(raw.decode())
        compiled = await compile_document(storage=storage, http=http, document=document,
                                          job=job, layout=layout)
        chunks = compiled.chunks
    except Exception as exc:
        await _fail_if_current(
            session, document.id, job.id, generation,
            f"编译版面失败：{type(exc).__name__}: {exc}", compile_failed=True)
        return 0

    user_id = job.initiated_by or document.uploaded_by
    if compiled.vision_requests:
        await record_usage(session, user_id=user_id, parse_job_id=job.id,
                           kind="compile_vision", requests=compiled.vision_requests)
        # VLM 已经产生真实成本；后续 embedding/落库失败也不能把流水回滚掉。
        await session.commit()

    if not chunks or not any(c.get("search_text") for c in chunks):
        # 纯图片/无文本层的文档：不是错误，但要让用户知道它不可问答
        await _fail_if_current(session, document.id, job.id, generation,
                               "文档没有可检索的文本块（可能是纯图片扫描件）")
        return 0

    try:
        vectors = await _embed_sparse(http, [c.get("search_text") or "" for c in chunks])
    except Exception as exc:
        await _fail_if_current(
            session, document.id, job.id, generation,
            f"向量化失败：{type(exc).__name__}: {exc}", layout=layout, compiled=compiled)
        return 0


    await record_usage(session, user_id=user_id, parse_job_id=job.id, kind="embed",
                       requests=_batch_count(sum(bool((c.get("search_text") or "").strip())
                                                 for c in chunks)))
    # 与 VLM 同理：向量已经算完，之后若版本切换或 DB 落库失败，成本仍须可审计。
    await session.commit()
    # 编译/VLM/embedding 可能很久。真正替换索引前重新锁住 Document，防止旧 worker
    # 在用户切换版本后晚提交，把 chunks 覆盖回旧 parse job。
    document = (await session.execute(
        select(Document).where(Document.id == document_id).with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if (document is None or document.current_job_id != job.id
            or document.index_status != "indexing"
            or document.index_generation != generation):
        await session.rollback()
        return 0

    # 先删后插：重解析后残留的旧块会被检索命中，且 seq 会错位
    await session.execute(delete(Chunk).where(Chunk.document_id == document.id))
    rows = await _materialize_evidence(session, document, job, compiled)
    session.add_all([
        Chunk(
            document_id=document.id, parse_job_id=job.id, seq=c["seq"],
            page_idx=c["page_idx"], bbox=c["bbox"], page_size=c["page_size"],
            text=c["text"], search_text=c.get("search_text") or "",
            derived_text=c.get("derived_text"), char_len=c["char_len"],
            block_type=c.get("block_type", "text"), table_html=c.get("table_html"),
            text_tokenized=c.get("text_tokenized", ""), provider=c.get("provider") or {},
            provider_fingerprint=c.get("provider_fingerprint") or "",
            evidence_id=rows[c["seq"]][0].id,
            derived_evidence_id=(rows[c["seq"]][1].id if rows[c["seq"]][1] else None),
            embedding=vec,
        )
        for c, vec in zip(chunks, vectors)
    ])
    _record_compile_state(document, layout, compiled)
    document.index_status = "ready"
    document.index_error = None
    document.index_lease_until = None
    document.updated_at = utcnow()
    await session.commit()
    return len(chunks)


async def _renew_lease_once(factory, document_id: str, generation: int) -> bool:
    """续一次租；这一代已被别人接管则返回 False。"""
    async with factory() as heartbeat:
        result = await heartbeat.execute(
            update(Document).where(
                Document.id == document_id,
                Document.index_status == "indexing",
                Document.index_generation == generation,
            ).values(index_lease_until=_lease_deadline(), updated_at=utcnow())
        )
        await heartbeat.commit()
        return result.rowcount > 0


async def _heartbeat_lease(bind, document_id: str, generation: int) -> None:
    """独立事务续租；worker 消失后 lease 自然过期，对账可接管。

    **取消必须落在 sleep 上，不能落在语句中间。** `index_document` 的 finally
    每轮都会 cancel 这个任务，而在 DBAPI 调用中途被取消时，连接池只能把那条
    连接判成状态不明并作废 —— 生产上是每轮索引白扔一条池连接，单测里更狠：
    共享的 in-memory SQLite 连接一作废，整个库连表带数据一起消失
    （`test_active_index_worker_renews_lease` 因此单独跑 8 次红 6 次）。
    shield 让在飞的那次续租跑完，取消在下一个 await 处生效。
    """
    factory = async_sessionmaker(bind, expire_on_commit=False)
    while True:
        await asyncio.sleep(settings.index_heartbeat_seconds)
        if not await asyncio.shield(_renew_lease_once(factory, document_id, generation)):
            return


async def _embed_all(http: httpx.AsyncClient, texts: list[str]) -> list[list[float]]:
    """分批向量化。批大小必须低于运行时的 max-client-batch-size，否则整批被拒。"""
    vectors: list[list[float]] = []
    size = settings.embedding_batch_size
    for start in range(0, len(texts), size):
        vectors.extend(await embed_texts(http, texts[start:start + size]))
    return vectors


async def _embed_sparse(http: httpx.AsyncClient, texts: list[str]) -> list[list[float] | None]:
    """空视觉原子没有可嵌入内容，保留 Evidence/Chunk 但 embedding 明确为 NULL。"""
    positions = [i for i, value in enumerate(texts) if value.strip()]
    embedded = await _embed_all(http, [texts[i] for i in positions]) if positions else []
    out: list[list[float] | None] = [None] * len(texts)
    for index, vector in zip(positions, embedded):
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


def _record_compile_state(document: Document, layout: dict, compiled: CompileOutput) -> None:
    document.compile_status = "partial" if compiled.degraded else "ready"
    document.compile_degraded = compiled.degraded
    document.compile_fingerprint = fingerprint(compiled.provider)
    document.layout_version = str(layout.get("layout_version") or "")
    document.code_detection = code_detection_of(layout)


def _batch_count(total: int) -> int:
    size = settings.embedding_batch_size
    return (total + size - 1) // size


async def _fail_if_current(session: AsyncSession, document_id: str, job_id: str | None,
                           generation: int, reason: str, *, compile_failed: bool = False,
                           layout: dict | None = None,
                           compiled: CompileOutput | None = None) -> None:
    values: dict = {"index_status": "failed", "index_error": reason,
                    "index_lease_until": None,
                    "updated_at": utcnow()}
    if compile_failed:
        values.update(compile_status="failed", compile_degraded=["compile_failed"])
    elif layout is not None and compiled is not None:
        values.update(
            compile_status="partial" if compiled.degraded else "ready",
            compile_degraded=compiled.degraded,
            compile_fingerprint=fingerprint(compiled.provider),
            layout_version=str(layout.get("layout_version") or ""),
            code_detection=code_detection_of(layout),
        )
    await session.execute(
        update(Document).where(Document.id == document_id,
                               Document.current_job_id == job_id,
                               Document.index_generation == generation,
                               Document.index_status == "indexing").values(**values)
    )
    await session.commit()
