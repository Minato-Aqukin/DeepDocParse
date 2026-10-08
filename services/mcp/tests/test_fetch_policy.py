"""MCP 出站抓取配置装配的单测：判据住在 `ddp_core.fetch_policy`，这里只钉装配。

- `/files/` 段边界、元数据 IP 封锁、逐跳重验等判据见
  `python/ddp_core/tests/test_fetch_policy.py`（唯一套件）；
- 端到端行为（图片 302 到内网拒收、DNS 重绑到元数据拒收）见
  `test_ask_document.py`。
"""
from ddp_mcp import server


def test_fetch_policy_config_defaults_closed(monkeypatch):
    monkeypatch.delenv("FETCH_TRUSTED_BASE", raising=False)
    monkeypatch.delenv("FETCH_ALLOW_REDIRECTS", raising=False)
    cfg = server._fetch_policy_config()
    assert cfg.trusted_bases == ()
    assert cfg.allow_redirects is False


def test_fetch_policy_config_reads_env(monkeypatch):
    monkeypatch.setenv(
        "FETCH_TRUSTED_BASE",
        "http://control-api:8080/files/, http://other:9/files/",
    )
    monkeypatch.setenv("FETCH_ALLOW_REDIRECTS", "true")
    cfg = server._fetch_policy_config()
    assert cfg.trusted_bases == (
        "http://control-api:8080/files/", "http://other:9/files/")
    assert cfg.allow_redirects is True
    assert cfg.trusted_bases != ()
