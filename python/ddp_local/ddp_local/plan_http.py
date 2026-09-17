"""Fixed authenticated user actions for local plan review; never remote admission."""
from typing import Any, Literal

from fastapi import APIRouter, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from ddp_core.application.plans import reject
from ddp_local.federation_dispatch import (
    CenterFault,
    delivery_result_bytes,
    federation_summary,
    resolve_center,
    resolve_center_ref,
)
from ddp_local.plan_templates import center_query_scope


class PreparePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_spec: dict[str, Any]
    plan: dict[str, Any]
    input_manifest: list[dict[str, Any]] = Field(max_length=1000)
    payload_bindings: list[dict[str, Any]] = Field(max_length=1000)
    output_locations: list[str] = Field(min_length=1, max_length=100)
    retention: str
    exploration: dict[str, Any]


class ProposalCenter(BaseModel):
    """Public paired transport binding. Never a credential."""

    model_config = ConfigDict(extra="forbid")
    recipient_node_id: str = Field(min_length=3, max_length=64)
    environment_id: str = Field(min_length=1, max_length=128)
    workspace_id: str = Field(min_length=1, max_length=128)
    profile_id: str = Field(min_length=1, max_length=128)
    issuer: str = Field(min_length=1, max_length=128)
    subject: str = Field(min_length=1, max_length=128)
    endpoint: str = Field(min_length=1, max_length=2048)


class ProposalInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ref: str = Field(min_length=1, max_length=128)
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    size_bytes: StrictInt = Field(ge=1)


class ProposePlan(BaseModel):
    """`center_query` template request; everything else is fixed by the template."""

    model_config = ConfigDict(extra="forbid")
    center: ProposalCenter
    query: str = Field(min_length=1, max_length=4096)
    inputs: list[ProposalInput] = Field(max_length=20)
    retention: Literal["temporary", "task_pinned"]
    valid_seconds: StrictInt = Field(ge=300, le=86400)


class ApprovePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phase: str
    confirmed_scope_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    user_confirmed: StrictBool


class CenterBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    center_ref: str | None = Field(default=None, min_length=1, max_length=64)
    endpoint: str | None = Field(default=None, min_length=1, max_length=2048)
    credential: str | None = Field(default=None, min_length=1, max_length=4096)
    timeout_seconds: float | None = Field(default=None, gt=0, le=600)


class DispatchPlan(BaseModel):
    """`POST /dispatch` 请求体。

    凭据两种给法：内联 `endpoint` + `credential`（只驻留内存，绝不落库、绝不
    回显），或给 `center_ref` 让工作区本地登记的 `federation-centers.json`
    （其次 `DDP_FEDERATION_CENTERS`）解析 —— 同时给时以 `center_ref` 为准。
    请求体和已持久状态里都不会出现 credential；`reconcile`/`delivery` 可以只给
    `center_ref`（或省略，沿用派发时记录的引用）。
    """

    model_config = ConfigDict(extra="forbid")
    center: CenterBinding
    phase: str = Field(pattern=r"^(exploration|execution)$")
    workspace: str | None = Field(default=None, min_length=1, max_length=128)
    scope_manifest: dict[str, Any] | None = None


class CenterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    center: CenterBinding | None = None
    workspace: str | None = Field(default=None, min_length=1, max_length=128)


class DeliveryAck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    delivery_id: str = Field(min_length=1, max_length=255)
    result_manifest_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    center: CenterBinding | None = None
    workspace: str | None = Field(default=None, min_length=1, max_length=128)


def plan_router(runtime):
    router = APIRouter(prefix="/api/v1/plans")
    # This listener has one authenticated workspace owner. Neither the body nor
    # model-produced TaskSpec is allowed to select the signing identity.
    identity = {"environment_id": runtime.store.environment_id,
                "workspace_id": runtime.store.workspace_id,
                "subject": "workspace:" + runtime.store.workspace_id}

    def operation_key(request):
        keys = request.headers.getlist("idempotency-key")
        if len(keys) != 1:
            reject("invalid_key", "one unambiguous Idempotency-Key is required")
        return keys[0]

    def actor_headers(request):
        # 只把调用者的 X-DDP-* 上下文转给中心；本地会话 Authorization 绝不外传。
        return {name: value for name, value in request.headers.items()
                if name.lower().startswith("x-ddp-")}

    def check_workspace(workspace):
        if workspace is not None and workspace != runtime.store.workspace_id:
            reject("unauthorized", "workspace differs from this listener")

    def center_config(binding, plan_id=None):
        if binding is not None:
            return resolve_center(runtime, center_ref=binding.center_ref,
                                  endpoint=binding.endpoint, credential=binding.credential,
                                  timeout_seconds=binding.timeout_seconds)
        state = runtime.federation_state(plan_id) if plan_id else None
        ref = state.get("center_ref") if state else None
        if ref is None:
            reject("invalid_plan", "provide center credentials or a configured center_ref")
        return resolve_center_ref(runtime, ref)

    def check_imported(inputs):
        # Local input refs refer to immutable imported versions, not arbitrary
        # paths or client-declared remote authority. Remote inputs need a trusted
        # provider adapter with a source policy resolver before they can prepare.
        for item in inputs:
            if set(item) != {"ref", "digest", "size_bytes"} or not isinstance(item.get("ref"), str):
                reject("input_changed", "fixed inputs must name imported local versions")
            version = runtime.store.version(item["ref"])
            if item["digest"] != "sha256:" + version["source_digest"] or item["size_bytes"] != version["size_bytes"]:
                reject("input_changed", "input manifest differs from the imported snapshot")

    @router.post("/prepare", status_code=201)
    async def prepare(body: PreparePlan, request: Request):
        scope = body.model_dump()
        check_imported(scope["input_manifest"])
        return runtime.consents.prepare(identity, scope, operation_key=operation_key(request))

    @router.post("/propose", status_code=201)
    async def propose(body: ProposePlan, request: Request):
        typed = body.model_dump()
        check_imported(typed["inputs"])

        def build(value, now):
            return center_query_scope(value, local_node_id=runtime.store.environment_id,
                                      workspace_id=runtime.store.workspace_id, now=now)

        return runtime.consents.propose(identity, typed, build, operation_key=operation_key(request))

    @router.get("")
    async def list_plans(limit: int = 50):
        listing = runtime.consents.list_plans(identity, limit=limit)
        for item in listing["items"]:
            item["federation"] = federation_summary(runtime, item["plan_id"])
        return listing

    @router.get("/{plan_id}")
    async def get(plan_id: str):
        return runtime.consents.get(identity, plan_id)

    @router.post("/{plan_id}/approve")
    async def approve(plan_id: str, body: ApprovePlan, request: Request):
        return runtime.consents.approve(identity, plan_id, **body.model_dump(), operation_key=operation_key(request))

    @router.post("/{plan_id}/revoke")
    async def revoke(plan_id: str, request: Request):
        return runtime.consents.revoke(identity, plan_id, operation_key=operation_key(request))

    @router.post("/{plan_id}/dispatch")
    async def dispatch(plan_id: str, body: DispatchPlan, request: Request):
        check_workspace(body.workspace)
        config = center_config(body.center)
        try:
            return await runtime.federation_dispatch(
                plan_id, config, phase=body.phase, operation_key=operation_key(request),
                actor_headers=actor_headers(request), scope_manifest=body.scope_manifest,
                center_ref=body.center.center_ref,
            )
        except CenterFault as exc:
            return center_fault(plan_id, exc)

    def center_fault(plan_id, exc):
        # 未知/被拒不是本地 HTTP 失败：返回已落库的派发状态 + 机器码，让 UI 去对账。
        state = runtime.federation_state(plan_id)
        return {**state, "error": {"code": exc.code, "status": exc.status,
                                   "retryable": exc.retryable}}

    @router.post("/{plan_id}/reconcile")
    async def reconcile(plan_id: str, request: Request, body: CenterRequest | None = None):
        check_workspace(body.workspace if body else None)
        config = center_config(body.center if body else None, plan_id)
        try:
            return await runtime.federation_reconcile(plan_id, config, actor_headers=actor_headers(request))
        except CenterFault as exc:
            return center_fault(plan_id, exc)

    @router.post("/{plan_id}/delivery/fetch")
    async def fetch_delivery(plan_id: str, request: Request, body: CenterRequest | None = None):
        check_workspace(body.workspace if body else None)
        config = center_config(body.center if body else None, plan_id)
        try:
            return await runtime.federation_fetch_delivery(plan_id, config, actor_headers=actor_headers(request))
        except CenterFault as exc:
            return center_fault(plan_id, exc)

    @router.post("/{plan_id}/delivery/ack")
    async def delivery_ack(plan_id: str, body: DeliveryAck, request: Request):
        check_workspace(body.workspace)
        config = center_config(body.center, plan_id)
        keys = request.headers.getlist("idempotency-key")
        if len(keys) > 1:
            reject("invalid_key", "at most one unambiguous Idempotency-Key is allowed")
        try:
            return await runtime.federation_confirm_delivery(
                plan_id, body.delivery_id, body.result_manifest_digest, config,
                actor_headers=actor_headers(request), operation_key=keys[0] if keys else None,
            )
        except CenterFault as exc:
            # A lost ack reply stays pending locally with a visible code; a new
            # key can re-send the center's idempotent ack.
            return center_fault(plan_id, exc)

    @router.get("/{plan_id}/delivery/result")
    async def delivery_result(plan_id: str):
        # Exact canonical bytes: the caller rehashes these against
        # result_manifest_digest instead of trusting the stored `verified` flag.
        return Response(delivery_result_bytes(runtime, plan_id), media_type="application/json")

    @router.get("/{plan_id}/federation")
    async def federation(plan_id: str):
        return runtime.federation_state(plan_id)

    return router
