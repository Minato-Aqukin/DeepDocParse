"""Fixed authenticated user actions for local plan review; never remote admission."""
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from ddp_core.application.plans import reject
from ddp_local.federation_dispatch import (
    CenterFault,
    resolve_center,
    resolve_center_ref,
)


class PreparePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_spec: dict[str, Any]
    plan: dict[str, Any]
    input_manifest: list[dict[str, Any]] = Field(max_length=1000)
    payload_bindings: list[dict[str, Any]] = Field(max_length=1000)
    output_locations: list[str] = Field(min_length=1, max_length=100)
    retention: str
    exploration: dict[str, Any]


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

    @router.post("/prepare", status_code=201)
    async def prepare(body: PreparePlan, request: Request):
        scope = body.model_dump()
        # Local input refs refer to immutable imported versions, not arbitrary
        # paths or client-declared remote authority. Remote inputs need a trusted
        # provider adapter with a source policy resolver before they can prepare.
        for item in scope["input_manifest"]:
            if set(item) != {"ref", "digest", "size_bytes"} or not isinstance(item.get("ref"), str):
                reject("input_changed", "fixed inputs must name imported local versions")
            version = runtime.store.version(item["ref"])
            if item["digest"] != "sha256:" + version["source_digest"] or item["size_bytes"] != version["size_bytes"]:
                reject("input_changed", "input manifest differs from the imported snapshot")
        return runtime.consents.prepare(identity, scope, operation_key=operation_key(request))

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
            # 未知/被拒不是本地 HTTP 失败：返回已落库的派发状态 + 机器码，让 UI 去对账。
            state = runtime.federation_state(plan_id)
            return {**state, "error": {"code": exc.code, "status": exc.status,
                                       "retryable": exc.retryable}}

    @router.post("/{plan_id}/reconcile")
    async def reconcile(plan_id: str, request: Request, body: CenterRequest | None = None):
        check_workspace(body.workspace if body else None)
        config = center_config(body.center if body else None, plan_id)
        return await runtime.federation_reconcile(plan_id, config, actor_headers=actor_headers(request))

    @router.post("/{plan_id}/delivery/fetch")
    async def fetch_delivery(plan_id: str, request: Request, body: CenterRequest | None = None):
        check_workspace(body.workspace if body else None)
        config = center_config(body.center if body else None, plan_id)
        return await runtime.federation_fetch_delivery(plan_id, config, actor_headers=actor_headers(request))

    @router.post("/{plan_id}/delivery/ack")
    async def delivery_ack(plan_id: str, body: DeliveryAck, request: Request):
        check_workspace(body.workspace)
        config = center_config(body.center, plan_id)
        return await runtime.federation_confirm_delivery(
            plan_id, body.delivery_id, body.result_manifest_digest, config,
            actor_headers=actor_headers(request),
        )

    @router.get("/{plan_id}/federation")
    async def federation(plan_id: str):
        return runtime.federation_state(plan_id)

    return router
