"""对象存储的**部署形态**守卫。

住在这里的是同一类问题：签名覆盖 host 与 scheme，而内网与公网这两样都可能
不一样 —— 用错了不会报错，只会让浏览器那一侧默默不能用。
"""
import pytest

from ddp_corpus import storage as storage_mod
from ddp_corpus.config import settings

BUCKET = "deepdocparse"
KEY = "objects/a"


@pytest.fixture
def endpoints(monkeypatch):
    monkeypatch.setattr(settings, "minio_internal_endpoint", "127.0.0.1:19000")
    monkeypatch.setattr(settings, "minio_public_endpoint", "ddp.example.com")
    monkeypatch.setattr(settings, "minio_access_key", "ak")
    monkeypatch.setattr(settings, "minio_secret_key", "sk")
    monkeypatch.setattr(settings, "minio_region", "us-east-1")
    monkeypatch.setattr(settings, "minio_bucket", BUCKET)


def test_public_secure_only_affects_the_browser_facing_client(endpoints, monkeypatch):
    """内网回环明文 + 公网隧道终结 TLS：两侧 scheme 必须能分开设。

    只有 `minio_secure` 一个开关时，这种形态无解 —— 关着的话给浏览器的
    预签名 URL 是 `http://`，而页面是 https 打开的，浏览器按混合内容拦掉
    上传与预览（**服务端零报错**）；开着的话内网 client 会去 https 连回环，
    `ensure_bucket` 当场连不上。
    """
    monkeypatch.setattr(settings, "minio_secure", False)
    monkeypatch.setattr(settings, "minio_public_secure", True)
    store = storage_mod.MinioStorage()

    public = store._public_client.presigned_get_object(BUCKET, KEY)
    assert public.startswith("https://ddp.example.com/"), public

    internal = store._client.presigned_get_object(BUCKET, KEY)
    assert internal.startswith("http://127.0.0.1:19000/"), internal


def test_public_secure_defaults_to_the_internal_scheme(endpoints, monkeypatch):
    """不设它时行为与从前逐字一致 —— 既有部署不受这次改动影响。"""
    monkeypatch.setattr(settings, "minio_secure", False)
    monkeypatch.setattr(settings, "minio_public_secure", None)
    store = storage_mod.MinioStorage()
    assert store._public_client.presigned_get_object(BUCKET, KEY).startswith("http://")

    monkeypatch.setattr(settings, "minio_secure", True)
    store = storage_mod.MinioStorage()
    assert store._public_client.presigned_get_object(BUCKET, KEY).startswith("https://")
