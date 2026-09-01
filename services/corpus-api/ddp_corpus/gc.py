"""对象回收：软删除的文档，其 MinIO 里的原件与归档产物要真的删掉。

放在对账循环里跑而不是单独起进程：它本来就是"扫库 + 补动作"，与对账同构。
删对象是不可逆的，所以只删已经软删除、且确实属于该文档的键前缀。

**与"删了又传回来"的竞态**：documents.upload 会把软删除的行复活并重新 put 原件。
本模块是全项目唯一一处会不可逆地毁数据的地方，因此两道防护：

1. 宽限期（gc_grace_seconds）—— 只回收"删掉有一阵子了"的文档。复活通常紧跟在
   误删之后，宽限期把撞车从真实竞态变成实际不可达。
2. claim —— 删对象之前先用条件 UPDATE 把这一行原子地移出"可回收"集合
   （沿用 archive/index 的套路）。已经提交的复活会让 claim 落空，GC 直接跳过；
   多副本也不会重复删。

残留窗口只剩"复活恰好提交在 claim 与删对象之间"这一瞬，且必须发生在删除满一个
宽限期之后 —— 接受它，换取"绝不把还在用的对象删掉"这个更重要的性质。
"""
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from ddp_corpus.config import settings
from ddp_corpus.models import Document, ParseJob, as_aware, utcnow
from ddp_corpus.storage import Storage, job_result_prefix, prefix_of


async def _claim(session, document: Document, object_key: str) -> bool:
    """把这一行移出"可回收"集合。抢不到（已被复活 / 被别的副本收走）返回 False。

    object_key 置空同时兼作"已回收"标记，所以 claim 与标记是同一次写入 ——
    不会出现"标记成功但对象没删"或"对象删了但标记没落"的中间态。
    """
    claimed = await session.execute(
        update(Document)
        .where(Document.id == document.id,
               Document.deleted_at.is_not(None),          # 复活过就不该再删
               Document.object_key == object_key)          # 换过 key 也不该删
        .values(object_key="")
    )
    await session.commit()
    return claimed.rowcount > 0


async def collect_deleted_objects(sessionmaker: async_sessionmaker, storage: Storage,
                                  limit: int = 20) -> int:
    """清理软删除文档的对象，返回清掉的文档数。"""
    cleaned = 0
    async with sessionmaker() as session:
        # 宽限期在 Python 里判而不是写进 SQL：SQLite 存 naive、PG 存 aware，
        # 直接拿 aware 参数去比会两边行为不一（全项目统一走 as_aware，见 models）
        cutoff = utcnow() - timedelta(seconds=settings.gc_grace_seconds)
        candidates = (await session.execute(
            select(Document).where(Document.deleted_at.is_not(None),
                                   Document.object_key != "")
            .order_by(Document.deleted_at).limit(limit * 5)
        )).scalars().all()

        for document in candidates:
            if cleaned >= limit:
                break
            if as_aware(document.deleted_at) > cutoff:
                continue                       # 还在宽限期内，下一轮再说
            object_key = document.object_key
            if not await _claim(session, document, object_key):
                continue

            jobs = (await session.execute(
                select(ParseJob).where(ParseJob.document_id == document.id)
            )).scalars().all()
            try:
                for job in jobs:
                    # 两个前缀都要列：归档产物在 job.result_prefix 下（迁移过来的老 job
                    # 指向原 task_id），而问答的裁剪图是按 job.id 写的（crops.crop_key）。
                    # 迁移过的 job 这两者不是同一个前缀，只列一个就会漏删另一半
                    prefixes = {prefix_of(job), job_result_prefix(job.id)}
                    for prefix in prefixes:
                        for key in await storage.list_prefix(prefix):
                            await storage.delete(key)
                await storage.delete(object_key)
            except Exception as exc:
                # claim 已经把 object_key 清了，这里不回滚：宁可漏几个对象没删
                # （下面这行日志就是线索），也不要把标记退回去、让下一轮重新去删
                # 一个可能已经被复活的文档
                print(f"[gc] {document.id} partially collected: {type(exc).__name__}: {exc}")
            cleaned += 1
    return cleaned
