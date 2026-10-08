"""网关出站抓取配置装配的单测：判据住在 `ddp_core.fetch_policy`，这里只钉装配。

受信基座、元数据 IP 封锁、逐跳重验等判据见
`python/ddp_core/tests/test_fetch_policy.py`（唯一套件）；这里只钉 Settings 的
逗号分隔与 env 落点，私网 file_url 端到端 400 见 test_contract。
"""
from ddp_core.fetch_policy import FetchPolicyConfig

from ddp_gateway.config import Settings

BASE = "http://control-api:8080/files/"


def test_fetch_policy_config_snapshot(monkeypatch):
    monkeypatch.delenv("FETCH_TRUSTED_BASE", raising=False)
    monkeypatch.delenv("FETCH_ALLOW_REDIRECTS", raising=False)
    s = Settings(
        fetch_trusted_base=f"http://other:9/files/, {BASE}",
        fetch_allow_redirects=True,
    )
    cfg = s.fetch_policy_config()
    assert isinstance(cfg, FetchPolicyConfig)
    assert cfg.allow_redirects is True
    assert cfg.trusted_bases == ("http://other:9/files/", BASE)


def test_fetch_policy_config_prefers_live_env(monkeypatch):
    monkeypatch.setenv("FETCH_TRUSTED_BASE", BASE)
    monkeypatch.delenv("FETCH_ALLOW_REDIRECTS", raising=False)
    s = Settings(fetch_trusted_base="", fetch_allow_redirects=False)
    cfg = s.fetch_policy_config()
    assert cfg.trusted_bases == (BASE,)
