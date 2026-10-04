"""T64：批准成员的内存信任缓存须同时受全局条目与真实过期清理约束。"""
from types import SimpleNamespace

import httpx
import pytest

from ddp_corpus import node_auth


@pytest.fixture
def trust_fixture(monkeypatch):
    clock = [0.0]
    calls = []
    config = SimpleNamespace(federation_peer_key_cache_seconds=5,
                             federation_peer_key_cache_max_entries=16,
                             control_url="http://control", service_token="test-service-token")
    monkeypatch.setattr(node_auth, "settings", config)

    def respond(request):
        node = request.url.path.rsplit("/", 1)[-1]
        calls.append(node)
        return httpx.Response(200, json={"node_id": node, "state": "approved", "revision": 1})

    return clock, calls, config, httpx.MockTransport(respond)


async def test_many_distinct_expired_identities_do_not_accumulate(trust_fixture):
    clock, calls, _, transport = trust_fixture
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        source = node_auth.ControlPeerTrust(client, clock=lambda: clock[0])
        for index in range(50):
            clock[0] = index * 6
            assert (await source.trust(f"node-{index}"))["state"] == "approved"
            assert len(source._cache) == 1, "过期身份从未再访问，也必须被新身份读写清走"
        assert len(calls) == 50


async def test_live_global_cap_evicts_oldest_and_refetches(trust_fixture):
    clock, calls, config, transport = trust_fixture
    config.federation_peer_key_cache_seconds = 60
    config.federation_peer_key_cache_max_entries = 3
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        source = node_auth.ControlPeerTrust(client, clock=lambda: clock[0])
        for index in range(50):
            clock[0] = index
            await source.trust(f"node-{index}")
            assert len(source._cache) <= 3
        assert calls.count("node-0") == 1
        await source.trust("node-49")
        assert calls.count("node-49") == 1, "保留中的成员应复用"
        await source.trust("node-0")
        assert calls.count("node-0") == 2, "被驱逐成员重新查控制面，不伪造信任记录"
        assert len(source._cache) == 3


async def test_cache_hit_also_prunes_other_expired_identities(trust_fixture):
    clock, calls, _, transport = trust_fixture
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        source = node_auth.ControlPeerTrust(client, clock=lambda: clock[0])
        await source.trust("node-old")
        clock[0] = 4
        await source.trust("node-fresh")
        clock[0] = 5
        await source.trust("node-fresh")
        assert calls == ["node-old", "node-fresh"]
        assert set(source._cache) == {"node-fresh"}, "命中读也须清理其他已过期成员"


async def test_insertion_rechecks_expiry_after_control_roundtrip(trust_fixture):
    clock, _, config, transport = trust_fixture
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        source = node_auth.ControlPeerTrust(client, clock=lambda: clock[0])
        await source.trust("node-old")

        async def delayed(request):
            clock[0] = 6
            return httpx.Response(200, json={"node_id": "node-new", "state": "approved"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(delayed), trust_env=False) as delayed_client:
            source._http = delayed_client
            clock[0] = 4
            await source.trust("node-new")
        assert set(source._cache) == {"node-new"}, "请求等待期间过期的旧身份必须在插入前清走"
        source._http = client
        config.federation_peer_key_cache_seconds = 0
        await source.trust("node-third")
        assert not source._cache, "禁用复用后也不能留下过期/旧缓存"
