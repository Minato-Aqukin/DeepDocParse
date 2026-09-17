"""P5 错误去敏感化：别人的 id 与不存在的 id 必须长得一模一样。

存在性探测口（existence oracle）是这类系统最便宜的攻击面：拿一个猜到的
probe/admission/evidence id 去问，如果"不是你的"返回 403/409 而"不存在"
返回 404，攻击者就把整个 id 空间枚举出来了。本文件把四个端点按同一形状
钉死：

- **跨组织**：真实但属于别人 -> 与随机不存在的 id **同状态码、同 error.code**；
- **过期/已撤**：只有真正有权的调用方能看到 410（`probe_expired` /
  `catalog_snapshot_invalid` + `revoked_collection_ids`），别人看到的是
  通用的 404/410，且**不带**任何"这项存在过"的字段。

认证形态：联邦端点只认节点凭证（`peer_unauthenticated` 另有专测）。
调用方是受信任的远端节点（`PeerCaller` 现签），组织取自本节点控制面的
信任记录，不取自报头。"同组织另一个调用者"指同一签发节点的不同主体
（subject 不同 -> peer actor id 不同）；"别的组织"指另一个签发节点，其
信任记录组织为 other-org。

`source_revoked` 的生产者在 `federation.resolve_evidence`（来源被删除/撤下且
调用者本组织曾拥有它 -> 410）；这里同时钉它的**边界**：对没有授权路径的
调用方与其它组织，410 必须退化成与不存在引用同形的 404。

凭据纪律：错误消息里永远没有凭据。新模型下出站带的是
`X-DDP-Node-Credential` 头（单次签发的 Ed25519 凭证），入站错误回执不得
回显它的任何字节；旧的共享口令形态（`X-DDP-Peer-Token`）在生产档位下
不再被接受，同样不得出现在错误回执里。每条负向断言都附带不泄露检查。
"""
from __future__ import annotations

import hashlib
from datetime import timedelta

import pytest
from sqlalchemy import select

from conftest import ACTOR, ORG, actor_headers
from ddp_corpus.config import settings
from ddp_corpus.federation_models import (
    FederationAdmission,
    FederationExecution,
    FederationProbe,
)
from ddp_corpus.main import app
from ddp_corpus.models import Evidence, Resource, ResourceVersion, utcnow
from node_credentials_fixture import (
    OTHER_KEY,
    OTHER_NODE_ID,
    OTHER_PUBLIC_KEY,
    PEER_NODE_ID,
    PEER_PUBLIC_KEY,
    caller,
    install,
    trust_record,
)
from test_federation_probes import (
    BASE,
    NODE,
    PEER,
    configure_federation,
    headers,
    indexed_source,
    probe_body,
    publish_collection,
)


@pytest.fixture(autouse=True)
def _federation_config(monkeypatch):
    configure_federation(monkeypatch)
    monkeypatch.setattr(settings, "federation_peers", "")
    monkeypatch.setattr(settings, "federation_allow_loopback", False)


@pytest.fixture
def _peer_auth(monkeypatch, app_state):
    """本节点 NODE；PEER_NODE_ID 是控制面批准的同组织成员，组织取自信任记录。"""
    return install(monkeypatch, app, node_id=NODE, organization_id=ORG)


def _peer(client, **over):
    """一个同组织远端节点对本节点的调用方：每次调用现签一张凭证。"""
    return caller(client, audience_node_id=NODE, **over)


def _stranger(client, _peer_auth, **over):
    """另一个组织的远端节点：信任记录组织为 other-org。"""
    _peer_auth.records[OTHER_NODE_ID] = trust_record(
        OTHER_NODE_ID, OTHER_PUBLIC_KEY, organization_id="other-org",
        authority_node_id=NODE)
    return caller(client, audience_node_id=NODE, issuer_node_id=OTHER_NODE_ID,
                  key=OTHER_KEY, **over)


def _same_shape(one, other) -> None:
    """两条响应必须同状态码、同机器码 —— 这是"没有探测口"的可测判据。"""
    assert one.status_code == other.status_code, (one.text, other.text)
    assert one.json()["error"]["code"] == other.json()["error"]["code"], \
        (one.text, other.text)


def _assert_no_credential_leak(response, *callers) -> None:
    """错误回执里永远没有凭据：新旧两种形态都不许出现。

    新形态是各调用方现签的 `X-DDP-Node-Credential` token（`caller.tokens`）；
    旧形态是共享口令档位的 `X-DDP-Peer-Token`（`PEER`）。生产档位下旧头不被
    接受，但即使调用方顺手带上，服务端也不许把它回显出来。
    """
    text = response.text
    for peer_caller in callers:
        for token in peer_caller.tokens:
            assert token not in text, "节点凭证不得出现在错误回执里"
    assert PEER not in text, "旧共享口令不得出现在错误回执里"


def _probe_constraints(digest: str, *, root_task_id: str = "root-1") -> dict:
    """读探针/探针证据集的凭证约束：读操作必须带着当初那份需求修订。"""
    return {"root_task_id": root_task_id, "task_spec_digest": digest}


async def test_probe_and_evidence_set_ids_have_no_cross_org_existence_oracle(
        client, session, _peer_auth):
    _, version, *_ = await indexed_source(session)
    collection = await publish_collection(client, version)
    owner = _peer(client)
    body = probe_body(collection["collection_id"])
    created = await owner.post(f"{BASE}/probes", json_body=body,
                               headers={"Idempotency-Key": "desensitize-probe"})
    assert created.status_code == 201, created.text
    probe_id = created.json()["probe_id"]
    digest = body["task_spec_digest"]
    read_constraints = _probe_constraints(digest)

    got = await owner.get(f"{BASE}/probes/{probe_id}", constraints=read_constraints)
    assert got.status_code == 200, got.text

    stranger = _stranger(client, _peer_auth)
    foreign_real = await stranger.get(f"{BASE}/probes/{probe_id}",
                                      constraints=read_constraints)
    foreign_fake = await stranger.get(f"{BASE}/probes/{'0' * 24}",
                                      constraints=read_constraints)
    _same_shape(foreign_real, foreign_fake)
    assert foreign_real.json()["error"]["code"] == "probe_not_found"
    _assert_no_credential_leak(foreign_real, owner, stranger)
    _assert_no_credential_leak(foreign_fake, owner, stranger)

    set_ref = f"federation-probe:{probe_id}"
    owner_set = await owner.get(f"{BASE}/evidence-sets/{set_ref}",
                                constraints=read_constraints)
    assert owner_set.status_code == 200, owner_set.text
    foreign_set = await stranger.get(f"{BASE}/evidence-sets/{set_ref}",
                                     constraints=read_constraints)
    fake_set = await stranger.get(f"{BASE}/evidence-sets/{'0' * 24}",
                                  constraints=read_constraints)
    _same_shape(foreign_set, fake_set)
    assert foreign_set.json()["error"]["code"] == "evidence_set_not_found"
    _assert_no_credential_leak(foreign_set, owner, stranger)
    _assert_no_credential_leak(fake_set, owner, stranger)


async def test_admission_lookup_and_execution_status_have_no_cross_org_oracle(
        client, session, _peer_auth):
    from test_federation_admissions import admission_body

    _, version, *_ = await indexed_source(session)
    collection = await publish_collection(client, version)
    owner = _peer(client)
    body = admission_body(key="desensitize-task", root_task_id="task-1",
                          step_id="retrieve-1",
                          collection_id=collection["collection_id"])
    # 入站受理的协调者就是调用方自己：计划里写谁在协调，签名证明谁在发，
    # 两者必须是同一个节点，否则 403 credential_scope_denied。
    body["plan"]["root_coordinator_node_id"] = PEER_NODE_ID
    created = await owner.post(f"{BASE}/admissions", json_body=body,
                               headers={"Idempotency-Key": body["idempotency_key"]})
    assert created.status_code == 201, created.text
    receipt = created.json()
    executor_task_id = receipt["executor_task_id"]
    assert executor_task_id
    admission_key = body["idempotency_key"]
    bound = {"root_task_id": body["root_task_id"], "step_id": body["step_id"]}

    owner_lookup = await owner.post(
        f"{BASE}/admissions/lookup", json_body={"idempotency_key": admission_key},
        constraints=dict(bound))
    assert owner_lookup.status_code == 200, owner_lookup.text

    stranger = _stranger(client, _peer_auth)
    foreign_lookup = await stranger.post(
        f"{BASE}/admissions/lookup", json_body={"idempotency_key": admission_key},
        constraints=dict(bound))
    fake_lookup = await stranger.post(
        f"{BASE}/admissions/lookup", json_body={"idempotency_key": "never-seen-key"},
        constraints=dict(bound))
    _same_shape(foreign_lookup, fake_lookup)
    assert foreign_lookup.json()["error"]["code"] == "admission_not_found"
    _assert_no_credential_leak(foreign_lookup, owner, stranger)
    _assert_no_credential_leak(fake_lookup, owner, stranger)

    owner_execution = await owner.get(f"{BASE}/tasks/{executor_task_id}",
                                      constraints=dict(bound))
    assert owner_execution.status_code == 200, owner_execution.text
    foreign_execution = await stranger.get(f"{BASE}/tasks/{executor_task_id}",
                                           constraints=dict(bound))
    fake_execution = await stranger.get(f"{BASE}/tasks/{'0' * 24}",
                                        constraints=dict(bound))
    _same_shape(foreign_execution, fake_execution)
    assert foreign_execution.json()["error"]["code"] == "task_not_found"
    _assert_no_credential_leak(foreign_execution, owner, stranger)
    _assert_no_credential_leak(fake_execution, owner, stranger)


async def test_expired_probe_is_410_only_for_the_authorized_org(
        client, session, _peer_auth):
    _, version, *_ = await indexed_source(session)
    collection = await publish_collection(client, version)
    owner = _peer(client)
    body = probe_body(collection["collection_id"])
    created = await owner.post(f"{BASE}/probes", json_body=body,
                               headers={"Idempotency-Key": "desensitize-expired"})
    assert created.status_code == 201, created.text
    probe_id = created.json()["probe_id"]
    digest = body["task_spec_digest"]
    read_constraints = _probe_constraints(digest)
    row = await session.get(FederationProbe, probe_id)
    row.expires_at = utcnow() - timedelta(seconds=1)
    await session.commit()

    got = await owner.get(f"{BASE}/probes/{probe_id}", constraints=read_constraints)
    assert got.status_code == 410, got.text
    assert got.json()["error"]["code"] == "probe_expired"

    stranger = _stranger(client, _peer_auth)
    foreign = await stranger.get(f"{BASE}/probes/{probe_id}",
                                 constraints=read_constraints)
    assert foreign.status_code == 404, "过期细节不许泄露给别的组织"
    assert foreign.json()["error"]["code"] == "probe_not_found"
    _assert_no_credential_leak(foreign, owner, stranger)

    owner_set = await owner.get(
        f"{BASE}/evidence-sets/federation-probe:{probe_id}",
        constraints=read_constraints)
    assert owner_set.status_code == 410, owner_set.text
    assert owner_set.json()["error"]["code"] == "probe_expired"
    foreign_set = await stranger.get(
        f"{BASE}/evidence-sets/federation-probe:{probe_id}",
        constraints=read_constraints)
    assert foreign_set.status_code == 404, foreign_set.text
    assert foreign_set.json()["error"]["code"] == "evidence_set_not_found"
    _assert_no_credential_leak(foreign_set, owner, stranger)


async def test_resource_and_evidence_ids_have_no_cross_org_existence_oracle(
        client, session, _peer_auth):
    """locate / resolve 对"别人的真 id"与"不存在的 id"给同一形状。"""
    from test_federation_locate import private_source

    resource, version, _, _, evidence = await private_source(session)
    stranger = _stranger(client, _peer_auth)
    foreign_locate = await stranger.post(
        f"{BASE}/resources/locate",
        json_body={"resource_id": resource.id, "version_id": version.id})
    fake_locate = await stranger.post(
        f"{BASE}/resources/locate",
        json_body={"resource_id": "0" * 32, "version_id": "0" * 32})
    _same_shape(foreign_locate, fake_locate)
    assert foreign_locate.json()["error"]["code"] == "resource_not_found"
    _assert_no_credential_leak(foreign_locate, stranger)
    _assert_no_credential_leak(fake_locate, stranger)

    foreign_resolve = await stranger.post(
        f"{BASE}/results/resolve", json_body={"evidence_ref": evidence.id})
    fake_resolve = await stranger.post(
        f"{BASE}/results/resolve", json_body={"evidence_ref": "0" * 32})
    _same_shape(foreign_resolve, fake_resolve)
    assert foreign_resolve.json()["error"]["code"] == "evidence_not_found"
    _assert_no_credential_leak(foreign_resolve, stranger)
    _assert_no_credential_leak(fake_resolve, stranger)


async def test_revoked_source_has_no_oracle_for_peers_after_tombstone(
        client, session, _peer_auth):
    """映射自旧 `test_revoked_source_is_410_only_for_the_authorized_actor`。

    旧断言：本地用户所有者拿 410 `source_revoked`，别人同形 404。
    新断言：联邦面上**所有**调用方（含曾可见的同组织节点）都拿与伪造引用
    同形的 404 `evidence_not_found`。
    原因：节点凭证模型下远端主体是只读 `peer-*` viewer，无 principal，
    `federation._revoked_source` 永假；tombstone 后 peer 授权路径为空，
    410 的"曾拥有"分支对 peer 不可达。这是**更强的脱敏**（联邦面连 410
    探测口都不留），不是弱化：live 200 -> 404 的变化本身证明撤销生效。
    """
    from ddp_corpus.resources import tombstone_resource
    from test_federation_locate import private_source

    resource, _, _, _, evidence = await private_source(session)
    owner = _peer(client)
    live = await owner.post(f"{BASE}/results/resolve",
                            json_body={"evidence_ref": evidence.id})
    assert live.status_code == 200, live.text

    await tombstone_resource(session, resource)
    await session.commit()

    revoked = await owner.post(f"{BASE}/results/resolve",
                               json_body={"evidence_ref": evidence.id})
    assert revoked.status_code == 404, revoked.text
    assert revoked.json()["error"]["code"] == "evidence_not_found"
    _assert_no_credential_leak(revoked, owner)

    # 同组织、另一个主体：与伪造引用同形。
    same_org = _peer(client, subject="user-other")
    same_org_hit = await same_org.post(
        f"{BASE}/results/resolve", json_body={"evidence_ref": evidence.id})
    unknown = await same_org.post(
        f"{BASE}/results/resolve", json_body={"evidence_ref": "0" * 32})
    _same_shape(same_org_hit, unknown)
    assert same_org_hit.json()["error"]["code"] == "evidence_not_found"
    _assert_no_credential_leak(same_org_hit, owner, same_org)
    _assert_no_credential_leak(unknown, owner, same_org)

    # 别的组织同样只有 404。
    stranger = _stranger(client, _peer_auth)
    foreign = await stranger.post(
        f"{BASE}/results/resolve", json_body={"evidence_ref": evidence.id})
    foreign_unknown = await stranger.post(
        f"{BASE}/results/resolve", json_body={"evidence_ref": "0" * 32})
    _same_shape(foreign, foreign_unknown)
    assert foreign.json()["error"]["code"] == "evidence_not_found"
    _assert_no_credential_leak(foreign, owner, stranger)
    _assert_no_credential_leak(foreign_unknown, owner, stranger)


async def test_withdrawn_source_has_no_oracle_for_peers(
        client, session, _peer_auth):
    """映射自旧 `test_withdrawn_source_stops_new_authorization_for_the_owner`。

    旧断言：本地用户所有者的 resolve 变 410 `source_revoked`，别人同形 404。
    新断言：联邦面上所有调用方都拿与伪造引用同形的 404。
    原因：同上 —— peer viewer 在 withdrawn 资源上没有授权路径（`allowed`
    为空），`_authorization_withdrawn` 的 410 分支对 peer 不可达。行还在
    （下面断言 DB 行仍在）证明这不是"数据没建出来"的假绿，而是授权停止。
    """
    from test_federation_locate import private_source

    resource, _, _, _, evidence = await private_source(session)
    resource.publication = "withdrawn"
    await session.commit()

    owner = _peer(client)
    revoked = await owner.post(f"{BASE}/results/resolve",
                               json_body={"evidence_ref": evidence.id})
    assert revoked.status_code == 404, revoked.text
    assert revoked.json()["error"]["code"] == "evidence_not_found"
    _assert_no_credential_leak(revoked, owner)
    # 行还在，只是授权停了：不是"数据没建出来"的假绿。
    assert await session.get(Evidence, evidence.id) is not None

    same_org = _peer(client, subject="user-other")
    same_org_hit = await same_org.post(
        f"{BASE}/results/resolve", json_body={"evidence_ref": evidence.id})
    unknown = await same_org.post(
        f"{BASE}/results/resolve", json_body={"evidence_ref": "0" * 32})
    _same_shape(same_org_hit, unknown)
    assert same_org_hit.json()["error"]["code"] == "evidence_not_found"
    _assert_no_credential_leak(same_org_hit, owner, same_org)
    _assert_no_credential_leak(unknown, owner, same_org)


async def test_revoked_source_evidence_set_has_no_oracle_for_peers(
        client, session, _peer_auth):
    """映射自旧 `test_revoked_source_evidence_set_is_410_for_the_bound_caller`。

    旧断言：绑定调用方整集 410 `source_revoked`，别的调用者 404。
    新断言：联邦面上所有调用方都拿与不存在集合同形的 404
    `evidence_set_not_found`。
    原因：证据集内本地条目的重授权走同一 `resolve_evidence` 路径，peer
    viewer 的 `_revoked_source` 永假，tombstone 后整集对 peer 就是 404。
    live 200 -> 404 的变化证明撤销生效，不是假绿。
    """
    from ddp_corpus.resources import tombstone_resource

    _, version, *_ = await indexed_source(session)
    collection = await publish_collection(client, version)
    owner = _peer(client)
    body = probe_body(collection["collection_id"])
    created = await owner.post(f"{BASE}/probes", json_body=body,
                               headers={"Idempotency-Key": "desensitize-source-revoked"})
    assert created.status_code == 201, created.text
    probe_id = created.json()["probe_id"]
    digest = body["task_spec_digest"]
    read_constraints = _probe_constraints(digest)
    set_ref = created.json()["retrieval"]["evidence_set_ref"]
    assert set_ref
    got = await owner.get(f"{BASE}/evidence-sets/{set_ref}",
                          constraints=read_constraints)
    assert got.status_code == 200, got.text

    probe = await session.get(FederationProbe, probe_id)
    evidence_id = probe.result_json["evidence"][0]["evidence_id"]
    evidence = await session.get(Evidence, evidence_id)
    resource = await session.scalar(select(Resource).join(ResourceVersion).where(
        ResourceVersion.document_id == evidence.document_id))
    await tombstone_resource(session, resource)
    await session.commit()

    revoked = await owner.get(f"{BASE}/evidence-sets/{set_ref}",
                              constraints=read_constraints)
    assert revoked.status_code == 404, revoked.text
    # 绑定调用方走到集内条目重授权那一层才失败（条目级 404），而无权调用方
    # 在集合级就被挡掉（集级 404）—— 与旧 410/404 的分层对应：有权者看到更
    # 靠里的细节，但状态码都是 404，不给存在性探测口留状态码级信号。
    assert revoked.json()["error"]["code"] == "evidence_not_found"
    _assert_no_credential_leak(revoked, owner)

    same_org = _peer(client, subject="user-other")
    same_org_hit = await same_org.get(
        f"{BASE}/evidence-sets/{set_ref}",
        constraints=_probe_constraints(digest))
    assert same_org_hit.status_code == 404, same_org_hit.text
    assert same_org_hit.json()["error"]["code"] == "evidence_set_not_found"
    _assert_no_credential_leak(same_org_hit, owner, same_org)
    # 无权调用方之间：真集合与伪造集合同形。
    same_org_fake = await same_org.get(
        f"{BASE}/evidence-sets/federation-probe:{'0' * 24}",
        constraints=_probe_constraints(digest))
    _same_shape(same_org_hit, same_org_fake)


def _catalog_headers(who: str, org: str, scope_seed: str) -> dict[str, str]:
    return {
        **actor_headers("control-api", kind="service", organization_id=org, role="admin"),
        "X-DDP-Caller-Actor": who,
        "X-DDP-Caller-Kind": "user",
        "X-DDP-Caller-Role": "contributor",
        "X-DDP-Caller-Scope": "sha256:" + hashlib.sha256(scope_seed.encode()).hexdigest(),
        "X-DDP-Authority-Node": NODE,
    }


async def test_revoked_collection_is_visible_only_to_the_bound_caller(actor_client, session):
    """集合被撤权：绑定调用方看到 410 + revoked ids，别人只有通用 410。"""
    _, version, *_ = await indexed_source(session)
    collection = await publish_collection(actor_client, version)
    scope_id = "desensitize-scope"
    mine = _catalog_headers(ACTOR, ORG, "caller-one")
    created = await actor_client.get("/internal/federation/collections",
                                     params={"scope_id": scope_id}, headers=mine)
    assert created.status_code == 200, created.text
    snapshot = created.json()
    assert collection["collection_id"] in {
        item["collection_id"] for item in snapshot["collections"]}

    withdrawn = await actor_client.post(
        f"/api/v1/collections/{collection['collection_id']}/withdraw",
        headers={**headers(role="admin"), "Idempotency-Key": "desensitize-withdraw"},
        json={"expected_revision": collection["revision"]})
    assert withdrawn.status_code == 200, withdrawn.text

    bound = await actor_client.get(
        "/internal/federation/collections", headers=mine,
        params={"scope_id": scope_id, "snapshot_id": snapshot["snapshot_id"],
                "cursor": snapshot["first_cursor"]})
    assert bound.status_code == 410
    assert bound.json()["error"]["code"] == "catalog_snapshot_invalid"
    assert collection["collection_id"] in bound.json()["revoked_collection_ids"]

    foreign = await actor_client.get(
        "/internal/federation/collections",
        headers=_catalog_headers("mallory", "other-org", "caller-two"),
        params={"scope_id": scope_id, "snapshot_id": snapshot["snapshot_id"],
                "cursor": snapshot["first_cursor"]})
    assert foreign.status_code == 410
    assert foreign.json()["error"]["code"] == "catalog_snapshot_invalid"
    assert "revoked_collection_ids" not in foreign.json(), \
        "撤权明细只给绑定调用方，别人不该知道哪个集合出过事"
