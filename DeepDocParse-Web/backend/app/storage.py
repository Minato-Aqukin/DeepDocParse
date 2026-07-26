"""对象存储（MinIO）—— 永久归档层。

两个坑，都在这里处理掉：
1. minio SDK 是同步的 —— 全部丢到线程池，别阻塞事件循环
2. 预签名 URL 的签名覆盖 host：给浏览器的 URL 与给 service 的 URL host 不同，
   签名也就不同，必须用两个 client 分别生成（internal / public endpoint）。
   主链路的"service 下载原件"走的是本层的稳定 URL(/files/{token})，不用预签名；
   预签名只用于前端直接下载原件。

Storage 是 Protocol：单测注入内存实现，不需要真 MinIO。
"""
import asyncio
import io
from datetime import timedelta
from typing import Protocol

from minio import Minio

from app.config import settings


class Storage(Protocol):
    async def put(self, key: str, data: bytes, content_type: str) -> None: ...
    async def get(self, key: str) -> bytes: ...
    async def exists(self, key: str) -> bool: ...
    async def list_prefix(self, prefix: str) -> list[str]: ...
    async def presigned_get(self, key: str, expires_seconds: int = 3600) -> str: ...


class MinioStorage:
    def __init__(self) -> None:
        self._client = Minio(
            settings.minio_internal_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )
        # 只用于生成浏览器可达的预签名 URL（host 不同 -> 签名不同）
        self._public_client = Minio(
            settings.minio_public_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )
        self._bucket = settings.minio_bucket

    async def ensure_bucket(self) -> None:
        def _ensure() -> None:
            if not self._client.bucket_exists(self._bucket):
                self._client.make_bucket(self._bucket)

        await asyncio.to_thread(_ensure)

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        await asyncio.to_thread(
            self._client.put_object, self._bucket, key, io.BytesIO(data), len(data),
            content_type,
        )

    async def get(self, key: str) -> bytes:
        def _get() -> bytes:
            resp = self._client.get_object(self._bucket, key)
            try:
                return resp.read()
            finally:
                resp.close()
                resp.release_conn()

        return await asyncio.to_thread(_get)

    async def exists(self, key: str) -> bool:
        def _stat() -> bool:
            from minio.error import S3Error
            try:
                self._client.stat_object(self._bucket, key)
                return True
            except S3Error:
                return False

        return await asyncio.to_thread(_stat)

    async def list_prefix(self, prefix: str) -> list[str]:
        def _list() -> list[str]:
            return [obj.object_name
                    for obj in self._client.list_objects(self._bucket, prefix=prefix, recursive=True)]

        return await asyncio.to_thread(_list)

    async def presigned_get(self, key: str, expires_seconds: int = 3600) -> str:
        return await asyncio.to_thread(
            self._public_client.presigned_get_object, self._bucket, key,
            timedelta(seconds=expires_seconds),
        )


class MemoryStorage:
    """单测与本地无 MinIO 时的替身。"""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}

    async def ensure_bucket(self) -> None:
        return None

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        self.objects[key] = (data, content_type)

    async def get(self, key: str) -> bytes:
        if key not in self.objects:
            raise KeyError(key)
        return self.objects[key][0]

    async def exists(self, key: str) -> bool:
        return key in self.objects

    async def list_prefix(self, prefix: str) -> list[str]:
        return sorted(k for k in self.objects if k.startswith(prefix))

    async def presigned_get(self, key: str, expires_seconds: int = 3600) -> str:
        return f"memory://{key}"


def source_key(task_id: str, filename: str) -> str:
    return f"sources/{task_id}/{filename}"


def result_prefix(task_id: str) -> str:
    return f"results/{task_id}/"
