"""对象回收：软删除的文档，其 MinIO 里的原件与归档产物要真的删掉。

放在对账循环里跑而不是单独起进程：它本来就是"扫库 + 补动作"，与对账同构。
删对象是不可逆的，所以只删已经软删除、且确实属于该文档的键前缀。
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models import Document, ParseJob
from app.storage import Storage, job_result_prefix, prefix_of


async def collect_deleted_objects(sessionmaker: async_sessionmaker, storage: Storage,
                                  limit: int = 20) -> int:
    """清理软删除文档的对象，返回清掉的文档数。

    清完把 object_key 置空作为"已回收"的标记 —— 不再删一次，也不留悬空引用。
    """
    cleaned = 0
    async with sessionmaker() as session:
        documents = (await session.execute(
            select(Document).where(Document.deleted_at.is_not(None),
                                   Document.object_key != "").limit(limit)
        )).scalars().all()

        for document in documents:
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
                await storage.delete(document.object_key)
            except Exception as exc:      # 对象存储抖动：下一轮再来，不改标记
                print(f"[gc] {document.id} failed: {type(exc).__name__}: {exc}")
                continue
            document.object_key = ""
            cleaned += 1
        if cleaned:
            await session.commit()
    return cleaned
