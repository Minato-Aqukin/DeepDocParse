"""P5 coordinator slice; see ``ddp_corpus.federation_tasks`` package docstring."""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from ddp_corpus.deps import Actor

from ddp_corpus.errors import APIError
from ddp_core.application.ports import ApplicationError
from ddp_corpus.federation_models import FederationDelivery, FederationRequest
from ddp_corpus.models import as_aware, new_id
from ddp_corpus import catalog, federation
from ddp_core.application import plans
from datetime import datetime, timedelta

from ddp_corpus.federation_tasks.common import (
    DELIVERY_RESULT_MAX_BYTES,
    DELIVERY_TTL_SECONDS,
    _EVENT_DELIVERY_CONFIRMED,
    _EVENT_DELIVERY_EXPIRED,
    _append_event,
    _commit,
    _instant,
    _revocation_sweep,
)

def _bounded_delivery_document(document: dict) -> dict | None:
    """把可交付文档限进字节上限；超限返回 None（**不截断**）。

    截断文档再报一个覆盖截断后字节的摘要，等于让本地"校验通过"的是被改过的
    内容 —— 客户端会据此确认一份与中心实际结果不同的交付。超限就如实不交付。
    """
    try:
        size = len(plans.canonical_bytes(document))
    except ApplicationError:
        return None
    if size > DELIVERY_RESULT_MAX_BYTES:
        return None
    return document

async def _deliver_result(session: AsyncSession, row: FederationRequest, *,
                          document: dict, digest: str, now: datetime) -> FederationDelivery:
    """任务产生结果时建一条 delivery=pending，并持久化有界的交付文档。

    `document` 是结果的规范文档（**剔除摘要字段本身**）：读取端点把它作为
    `result` 返回，客户端用 `content_digest(canonical result)` 与
    `result_manifest_digest` 对账。retention 永远不是 persistent。
    """
    delivery = await session.get(FederationDelivery, row.delivery_id) if row.delivery_id else None
    if delivery is None or delivery.state in ("expired", "confirmed"):
        delivery_id = new_id()
        delivery = FederationDelivery(
            delivery_id=delivery_id, root_task_id=row.root_task_id, state="pending",
            result_manifest_digest=digest, retention="temporary",
            expires_at=now + timedelta(seconds=DELIVERY_TTL_SECONDS), receipt_json={},
            created_at=now, updated_at=now)
        session.add(delivery)
        row.delivery_id = delivery_id
    else:
        delivery.result_manifest_digest = digest
        delivery.expires_at = now + timedelta(seconds=DELIVERY_TTL_SECONDS)
        delivery.updated_at = now
    delivery.result_json = _bounded_delivery_document(document)
    delivery.verified_at = None
    row.delivery_state = "pending"
    return delivery

# ---------------------------------------------------------------------------
# 交付回执
# ---------------------------------------------------------------------------


def _delivery_receipt(delivery: FederationDelivery, *, idempotency_key: str) -> dict:
    return {
        "schema": "ddp-evidence/1#DeliveryReceipt",
        "delivery_id": delivery.delivery_id,
        "root_task_id": delivery.root_task_id,
        "step_id": "fuse-1",
        "state": delivery.state,
        "result_manifest_digest": delivery.result_manifest_digest,
        "verified_at": _instant(delivery.verified_at) if delivery.verified_at else None,
        "idempotency_key": idempotency_key,
        "retention": delivery.retention,
    }

async def ack_delivery(session: AsyncSession, actor: Actor, delivery_id: str, *,
                       result_manifest_digest: str, idempotency_key: str,
                       now: datetime) -> dict:
    """本地校验后的幂等确认。

    只有**摘要与已交付结果一致**才可能 confirmed；TTL 到期一律 expired，
    过期件永远不可确认（§8.3）。重复确认返回同一回执。
    每 delivery 的咨询锁把并发确认串行化：两个不同键的确认不许各写一份回执。
    """
    await catalog.lock_key(session, "federation-delivery:" + delivery_id)
    delivery = await session.get(FederationDelivery, delivery_id)
    row = await session.get(FederationRequest, delivery.root_task_id) if delivery else None
    # 与 read_delivery / _load_request 同一可见性判据：同组织的其他成员（非
    # 管理员）拿到 delivery_id 也不许替别人确认 —— 确认会把交付钉成
    # confirmed、绕过 TTL 过期。不可见与不存在同形 404。
    if delivery is None or row is None or row.organization_id != actor.organization_id \
            or (row.actor_id != federation.acting_actor(actor) and not actor.can_manage):
        raise APIError(404, "delivery not found", "invalid_request_error", "delivery_not_found")
    if delivery.state == "confirmed":
        if result_manifest_digest != delivery.result_manifest_digest:
            raise APIError(409, "confirmed delivery replay has a different request body",
                           "invalid_request_error", "idempotency_conflict")
        return delivery.receipt_json
    expired = delivery.state == "expired" or (
        delivery.expires_at is not None and as_aware(delivery.expires_at) <= now)
    key = str((delivery.receipt_json or {}).get("idempotency_key") or idempotency_key)
    if expired:
        delivery.state = "expired"
        delivery.updated_at = now
        row.delivery_state = "expired"
        row.updated_at = now
        delivery.receipt_json = _delivery_receipt(delivery, idempotency_key=key)
        await _append_event(session, row.root_task_id, _EVENT_DELIVERY_EXPIRED, {
            "delivery_id": delivery.delivery_id}, now=now)
        await _commit(session)
        return delivery.receipt_json
    if delivery.result_json is None:
        # 没有可校验的字节（超界未持久化、或历史交付）就不许确认：客户端
        # 下载不到 result，拿什么"本地校验过"都不成立。
        raise APIError(409, "delivery has no stored result bytes to verify",
                       "invalid_request_error", "input_not_verified")
    if result_manifest_digest != delivery.result_manifest_digest:
        raise APIError(409, "submitted result manifest digest does not match the delivered result",
                       "invalid_request_error", "input_not_verified")
    delivery.state = "confirmed"
    delivery.verified_at = now
    delivery.updated_at = now
    row.delivery_state = "confirmed"
    row.updated_at = now
    delivery.receipt_json = _delivery_receipt(delivery, idempotency_key=key)
    await _append_event(session, row.root_task_id, _EVENT_DELIVERY_CONFIRMED, {
        "delivery_id": delivery.delivery_id,
        "result_manifest_digest": delivery.result_manifest_digest}, now=now)
    await _commit(session)
    return delivery.receipt_json

async def read_delivery(session: AsyncSession, actor: Actor, delivery_id: str, *,
                        now: datetime) -> dict:
    """读取交付字节（有界 JSON）。**读取不是确认**。

    - 未知/不可见同形 404（不给出存在性探测口）；
    - 未确认且过 TTL：先把交付置 expired 并写事件，再回 410
      `delivery_expired` —— 本地据此把状态标成 expired，绝不显示"已保存"；
    - confirmed 是终态，不再受 TTL 影响（客户端已经校验并持有）；
    - `result` 是规范文档，`content_digest(canonical result)` 必须等于
      `result_manifest_digest`；超界未持久化时是 null，客户端必须拒绝确认。
    - 交付字节同样实时复查撤销：失效证据经交付暴露即 410 `source_revoked`。
    """
    delivery = await session.get(FederationDelivery, delivery_id)
    row = await session.get(FederationRequest, delivery.root_task_id) if delivery else None
    if delivery is None or row is None or row.organization_id != actor.organization_id \
            or (row.actor_id != federation.acting_actor(actor) and not actor.can_manage):
        raise APIError(404, "delivery not found", "invalid_request_error", "delivery_not_found")
    if delivery.state not in ("confirmed", "expired") \
            and delivery.expires_at is not None and as_aware(delivery.expires_at) <= now:
        delivery.state = "expired"
        delivery.updated_at = now
        row.delivery_state = "expired"
        row.updated_at = now
        await _append_event(session, row.root_task_id, _EVENT_DELIVERY_EXPIRED, {
            "delivery_id": delivery.delivery_id}, now=now)
        await _commit(session)
    if delivery.state == "expired":
        raise APIError(410, "delivery has expired and was never confirmed locally",
                       "invalid_request_error", "delivery_expired")
    fused = ((delivery.result_json or {}).get("evidence")) or []
    if fused:
        revoked = await _revocation_sweep(session, actor, fused=fused, now=now)
        if revoked is not None:
            raise APIError(410, "evidence source was revoked", "invalid_request_error",
                           "source_revoked")
    return {
        "delivery_id": delivery.delivery_id,
        "root_task_id": delivery.root_task_id,
        "state": delivery.state,
        "result_manifest_digest": delivery.result_manifest_digest,
        "result": delivery.result_json,
        "expires_at": _instant(delivery.expires_at) if delivery.expires_at else None,
    }
