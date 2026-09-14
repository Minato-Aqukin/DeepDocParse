"""P5 错误去敏感化：别人的 id 与不存在的 id 必须长得一模一样。

存在性探测口（existence oracle）是这类系统最便宜的攻击面：拿一个猜到的
probe/admission/evidence id 去问，如果"不是你的"返回 403/409 而"不存在"
返回 404，攻击者就把整个 id 空间枚举出来了。本文件把四个端点按同一形状
钉死：

- **跨组织**：真实但属于别人 -> 与随机不存在的 id **同状态码、同 error.code**；
- **过期/已撤**：只有真正有权的调用方能看到 410（`probe_expired` /
  `catalog_snapshot_invalid` + `revoked_collection_ids`），别人看到的是
  通用的 404/410，且**不带**任何"这项存在过"的字段。

`source_revoked` 的生产者在 `federation.resolve_evidence`（来源被删除/撤下且
调用者本组织曾拥有它 -> 410）；这里同时钉它的**边界**：对没有授权路径的
同组织用户与其它组织，410 必须退化成与不存在引用同形的 404。
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
from ddp_corpus.models import Evidence, Resource, ResourceVersion, utcnow
from test_federation_probes import (
    BASE,
    NODE,
    configure_federation,
    headers,
    indexed_source,
    post_probe,
    probe_body,
    publish_collection,
)
from test_federation_tasks import run_local_task


@pytest.fixture(autouse=True)
def _federation_config(monkeypatch):
    configure_federation(monkeypatch)
    monkeypatch.setattr(settings, "federation_peers", "")
    monkeypatch.setattr(settings, "federation_allow_loopback", False)


def _foreign() -> dict[str, str]:
    return headers("mallory", org="other-org")


def _same_shape(one, other) -> None:
    """两条响应必须同状态码、同机器码 —— 这是"没有探测口"的可测判据。"""
    assert one.status_code == other.status_code, (one.text, other.text)
    assert one.json()["error"]["code"] == other.json()["error"]["code"], \
        (one.text, other.text)


async def test_probe_and_evidence_set_ids_have_no_cross_org_existence_oracle(
        actor_client, session):
    _, version, *_ = await indexed_source(session)
    collection = await publish_collection(actor_client, version)
    created = await post_probe(actor_client, probe_body(collection["collection_id"]),
                               key="desensitize-probe")
    assert created.status_code == 201, created.text
    probe_id = created.json()["probe_id"]

    owner = await actor_client.get(f"{BASE}/probes/{probe_id}", headers=headers())
    assert owner.status_code == 200
    foreign_real = await actor_client.get(f"{BASE}/probes/{probe_id}", headers=_foreign())
    foreign_fake = await actor_client.get(f"{BASE}/probes/{'0' * 24}", headers=_foreign())
    _same_shape(foreign_real, foreign_fake)
    assert foreign_real.json()["error"]["code"] == "probe_not_found"

    set_ref = f"federation-probe:{probe_id}"
    owner_set = await actor_client.get(f"{BASE}/evidence-sets/{set_ref}", headers=headers())
    assert owner_set.status_code == 200, owner_set.text
    foreign_set = await actor_client.get(f"{BASE}/evidence-sets/{set_ref}", headers=_foreign())
    fake_set = await actor_client.get(f"{BASE}/evidence-sets/{'0' * 24}", headers=_foreign())
    _same_shape(foreign_set, fake_set)
    assert foreign_set.json()["error"]["code"] == "evidence_set_not_found"


async def test_admission_lookup_and_execution_status_have_no_cross_org_oracle(
        actor_client, session):
    run = await run_local_task(actor_client, session, key="desensitize-task")
    admission = await session.scalar(select(FederationAdmission).where(
        FederationAdmission.root_task_id == run["root"]))
    execution = await session.scalar(select(FederationExecution).where(
        FederationExecution.admission_id == admission.admission_id))
    assert admission is not None and execution is not None

    owner_lookup = await actor_client.post(f"{BASE}/admissions/lookup", headers=headers(),
                                           json={"idempotency_key": admission.idempotency_key})
    assert owner_lookup.status_code == 200, owner_lookup.text
    foreign_lookup = await actor_client.post(
        f"{BASE}/admissions/lookup", headers=_foreign(),
        json={"idempotency_key": admission.idempotency_key})
    fake_lookup = await actor_client.post(
        f"{BASE}/admissions/lookup", headers=_foreign(),
        json={"idempotency_key": "never-seen-key"})
    _same_shape(foreign_lookup, fake_lookup)
    assert foreign_lookup.json()["error"]["code"] == "admission_not_found"

    owner_execution = await actor_client.get(
        f"{BASE}/tasks/{execution.executor_task_id}", headers=headers())
    assert owner_execution.status_code == 200, owner_execution.text
    foreign_execution = await actor_client.get(
        f"{BASE}/tasks/{execution.executor_task_id}", headers=_foreign())
    fake_execution = await actor_client.get(f"{BASE}/tasks/{'0' * 24}", headers=_foreign())
    _same_shape(foreign_execution, fake_execution)
    assert foreign_execution.json()["error"]["code"] == "task_not_found"


async def test_expired_probe_is_410_only_for_the_authorized_org(actor_client, session):
    _, version, *_ = await indexed_source(session)
    collection = await publish_collection(actor_client, version)
    created = await post_probe(actor_client, probe_body(collection["collection_id"]),
                               key="desensitize-expired")
    probe_id = created.json()["probe_id"]
    row = await session.get(FederationProbe, probe_id)
    row.expires_at = utcnow() - timedelta(seconds=1)
    await session.commit()

    owner = await actor_client.get(f"{BASE}/probes/{probe_id}", headers=headers())
    assert owner.status_code == 410
    assert owner.json()["error"]["code"] == "probe_expired"

    foreign = await actor_client.get(f"{BASE}/probes/{probe_id}", headers=_foreign())
    assert foreign.status_code == 404, "过期细节不许泄露给别的组织"
    assert foreign.json()["error"]["code"] == "probe_not_found"

    owner_set = await actor_client.get(
        f"{BASE}/evidence-sets/federation-probe:{probe_id}", headers=headers())
    assert owner_set.status_code == 410
    assert owner_set.json()["error"]["code"] == "probe_expired"
    foreign_set = await actor_client.get(
        f"{BASE}/evidence-sets/federation-probe:{probe_id}", headers=_foreign())
    assert foreign_set.status_code == 404
    assert foreign_set.json()["error"]["code"] == "evidence_set_not_found"


async def test_resource_and_evidence_ids_have_no_cross_org_existence_oracle(
        actor_client, session):
    """locate / resolve 对"别人的真 id"与"不存在的 id"给同一形状。"""
    from test_federation_locate import private_source

    resource, version, _, _, evidence = await private_source(session)
    foreign_locate = await actor_client.post(
        f"{BASE}/resources/locate", headers=_foreign(),
        json={"resource_id": resource.id, "version_id": version.id})
    fake_locate = await actor_client.post(
        f"{BASE}/resources/locate", headers=_foreign(),
        json={"resource_id": "0" * 32, "version_id": "0" * 32})
    _same_shape(foreign_locate, fake_locate)
    assert foreign_locate.json()["error"]["code"] == "resource_not_found"

    foreign_resolve = await actor_client.post(
        f"{BASE}/results/resolve", headers=_foreign(), json={"evidence_ref": evidence.id})
    fake_resolve = await actor_client.post(
        f"{BASE}/results/resolve", headers=_foreign(), json={"evidence_ref": "0" * 32})
    _same_shape(foreign_resolve, fake_resolve)
    assert foreign_resolve.json()["error"]["code"] == "evidence_not_found"


async def test_revoked_source_is_410_only_for_the_authorized_actor(actor_client, session):
    """来源被真正撤销后：有授权路径的调用者拿 410 source_revoked，别人仍同形 404。

    撤销走生产路径 `tombstone_resource`（资源/版本/文档一起置删、publication 转
    withdrawn）。410 只对曾拥有这条来源的调用者出现 —— 对没有授权路径的人说
    "它被撤销了"，与承认它存在没有区别。`source_revoked` 第一次有了生产者。
    """
    from ddp_corpus.resources import tombstone_resource
    from test_federation_locate import private_source

    resource, _, _, _, evidence = await private_source(session)
    live = await actor_client.post(f"{BASE}/results/resolve", headers=headers(),
                                   json={"evidence_ref": evidence.id})
    assert live.status_code == 200, live.text

    await tombstone_resource(session, resource)
    await session.commit()

    revoked = await actor_client.post(f"{BASE}/results/resolve", headers=headers(),
                                      json={"evidence_ref": evidence.id})
    assert revoked.status_code == 410, revoked.text
    assert revoked.json()["error"]["code"] == "source_revoked"

    # 同组织、非所有者：从未有过授权路径，与伪造引用同形。
    same_org = await actor_client.post(f"{BASE}/results/resolve", headers=headers("bob"),
                                       json={"evidence_ref": evidence.id})
    unknown = await actor_client.post(f"{BASE}/results/resolve", headers=headers("bob"),
                                      json={"evidence_ref": "0" * 32})
    _same_shape(same_org, unknown)
    assert same_org.json()["error"]["code"] == "evidence_not_found"

    # 别的组织同样只有 404。
    foreign = await actor_client.post(f"{BASE}/results/resolve", headers=_foreign(),
                                      json={"evidence_ref": evidence.id})
    foreign_unknown = await actor_client.post(f"{BASE}/results/resolve", headers=_foreign(),
                                              json={"evidence_ref": "0" * 32})
    _same_shape(foreign, foreign_unknown)
    assert foreign.json()["error"]["code"] == "evidence_not_found"


async def test_withdrawn_source_stops_new_authorization_for_the_owner(actor_client, session):
    """显式撤下（publication=withdrawn、行还在）也停止新授权：

    所有者的 resolve 变 410，别人仍是同形 404 —— 与 `indexing` 对 withdrawn
    资源停新处理的判据一致（plan §4.4）。同一 parse 还有一条未撤下的绑定时
    绝不误报，由 `_authorization_withdrawn` 的 all() 判据保证。
    """
    from test_federation_locate import private_source

    resource, _, _, _, evidence = await private_source(session)
    resource.publication = "withdrawn"
    await session.commit()

    revoked = await actor_client.post(f"{BASE}/results/resolve", headers=headers(),
                                      json={"evidence_ref": evidence.id})
    assert revoked.status_code == 410, revoked.text
    assert revoked.json()["error"]["code"] == "source_revoked"

    same_org = await actor_client.post(f"{BASE}/results/resolve", headers=headers("bob"),
                                       json={"evidence_ref": evidence.id})
    unknown = await actor_client.post(f"{BASE}/results/resolve", headers=headers("bob"),
                                      json={"evidence_ref": "0" * 32})
    _same_shape(same_org, unknown)
    assert same_org.json()["error"]["code"] == "evidence_not_found"


async def test_revoked_source_evidence_set_is_410_for_the_bound_caller(actor_client, session):
    """证据集里的本地条目重新授权时看到撤销 -> 整集 410；别的调用者仍是 404。"""
    from ddp_corpus.resources import tombstone_resource

    run = await run_local_task(actor_client, session, key="desensitize-source-revoked")
    probe = await session.scalar(select(FederationProbe).where(
        FederationProbe.organization_id == ORG).execution_options(populate_existing=True))
    set_ref = probe.result_json["result"]["retrieval"]["evidence_set_ref"]
    assert set_ref
    owner = await actor_client.get(f"{BASE}/evidence-sets/{set_ref}", headers=headers())
    assert owner.status_code == 200, owner.text

    evidence = await session.get(Evidence, run["evidence_rows"][0].id)
    resource = await session.scalar(select(Resource).join(ResourceVersion).where(
        ResourceVersion.document_id == evidence.document_id))
    await tombstone_resource(session, resource)
    await session.commit()

    revoked = await actor_client.get(f"{BASE}/evidence-sets/{set_ref}", headers=headers())
    assert revoked.status_code == 410, revoked.text
    assert revoked.json()["error"]["code"] == "source_revoked"

    same_org = await actor_client.get(f"{BASE}/evidence-sets/{set_ref}", headers=headers("bob"))
    assert same_org.status_code == 404, same_org.text
    assert same_org.json()["error"]["code"] == "evidence_set_not_found"


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
