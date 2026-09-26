"""Fixed authenticated user actions for local plan review; never remote admission."""
from typing import Any, Literal

from fastapi import APIRouter, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator

from ddp_core.application.plans import reject
from ddp_local.federation_dispatch import (
    CenterFault,
    delivery_result_bytes,
    federation_summary,
    resolve_center,
    resolve_center_ref,
)
from ddp_local.plan_templates import center_file_scope, center_query_scope


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
    """Public paired transport binding. Never a credential.

    `upload_endpoint` is the optional paired object-upload origin fixed by the
    host pairing metadata (HTTPS bare origin, default = center origin). File
    plans bind it as a second `center-storage` transport; the renderer never
    supplies a URL.
    """

    model_config = ConfigDict(extra="forbid")
    recipient_node_id: str = Field(min_length=3, max_length=64)
    environment_id: str = Field(min_length=1, max_length=128)
    workspace_id: str = Field(min_length=1, max_length=128)
    profile_id: str = Field(min_length=1, max_length=128)
    issuer: str = Field(min_length=1, max_length=128)
    subject: str = Field(min_length=1, max_length=128)
    endpoint: str = Field(min_length=1, max_length=2048)
    upload_endpoint: str | None = Field(default=None, min_length=1, max_length=2048)


class ProposalInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ref: str = Field(min_length=1, max_length=128)
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    size_bytes: StrictInt = Field(ge=1)


class ProposePlan(BaseModel):
    """`center_query` template request; everything else is fixed by the template.

    `template=center_only` (default): exactly the paired center. `template=
    trusted_federation`: frozen `recipients` (1..100 node ids, must contain
    the center recipient; resolved by native host from paired connections,
    never renderer-supplied URLs) plus optional `scope_manifest` mirror only
    (center-returned verbatim; never trusted as authority).
    """
    model_config = ConfigDict(extra="forbid")
    center: ProposalCenter
    query: str = Field(min_length=1, max_length=4096)
    purpose: Literal["answer", "wiki"] = "answer"
    wiki: dict[str, Any] | None = None
    inputs: list[ProposalInput] = Field(max_length=20)
    retention: Literal["temporary", "task_pinned"]
    valid_seconds: StrictInt = Field(ge=300, le=86400)
    template: Literal["center_only", "trusted_federation"] = "center_only"
    recipients: list[str] | None = Field(default=None, min_length=1, max_length=100)
    scope_manifest: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _check_wiki_intent(self):
        # purpose=wiki requires the typed wiki request (wiki.pages +
        # requirements.wiki); answer must never carry it. Extra wiki keys are
        # rejected by the template's requirements_wiki check, not silently cut.
        if self.purpose == "wiki" and self.wiki is None:
            raise ValueError("purpose=wiki requires a typed wiki request")
        if self.purpose != "wiki" and self.wiki is not None:
            raise ValueError("requirements.wiki is only valid for wiki.pages")
        if isinstance(self.wiki, dict):
            extra = set(self.wiki) - {"title", "max_pages"}
            if extra:
                raise ValueError("typed wiki request carries unknown fields")
        return self


class ProposeFilePlan(BaseModel):
    """`center_file_parse` template request: one pinned local file to one paired center.

    The renderer names only the paired center, a filename label and the pinned
    digest/size of an already imported local version. The template fixes the
    `corpus.parse` operation, edges, budget and retention; the ledger rechecks
    the snapshot on approval and dispatch. The old query path stays available
    but never impersonates file compute.
    """

    model_config = ConfigDict(extra="forbid")
    center: ProposalCenter
    filename: str = Field(min_length=1, max_length=255)
    inputs: list[ProposalInput] = Field(min_length=1, max_length=1)
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


class TransferAuthorize(BaseModel):
    """Host-only per-action upload authorization (never in renderer whitelist).

    `action`: create|resume|part|finalize. `upload_id` binds the recorded
    control upload; `offset`/`length` only for part transfers. Returns a fixed
    host-only ticket (remote_compute_id, input_ref, filename, input_sha256
    bare hex, input_size, recipient_node_id, retention, upload_id) with no
    credential, path or URL.
    """

    model_config = ConfigDict(extra="forbid")
    center: CenterBinding
    action: str = Field(pattern=r"^(create|resume|part|finalize)$")
    upload_id: str | None = Field(default=None, min_length=1, max_length=128)
    offset: int | None = Field(default=None, ge=0)
    length: int | None = Field(default=None, gt=0)
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
        from ddp_local.plan_templates import center_trusted_query_scope
        typed = body.model_dump()
        check_imported(typed["inputs"])
        def build(value, now):
            if (value.get("template") or "center_only") == "trusted_federation":
                return center_trusted_query_scope(value, local_node_id=runtime.store.environment_id,
                                                  workspace_id=runtime.store.workspace_id, now=now)
            return center_query_scope(value, local_node_id=runtime.store.environment_id,
                                      workspace_id=runtime.store.workspace_id, now=now)
        return runtime.consents.propose(identity, typed, build, operation_key=operation_key(request))

    @router.post("/{plan_id}/review-center", status_code=201)
    async def review_center(plan_id: str, request: Request):
        """Mint a reviewed child scope bound to C's persisted plan mirror.

        Body is empty ({}): only the Idempotency-Key header (operation_key)
        is read. Returns the new prepared child plan view (same shape as
        propose); it carries no execution consent. Renderer must never
        supply C plans/URLs/credentials.
        """
        from ddp_local.federation_dispatch import review_center_plan
        return review_center_plan(runtime, plan_id, operation_key=operation_key(request))
    @router.post("/propose-file", status_code=201)
    async def propose_file(body: ProposeFilePlan, request: Request):
        typed = body.model_dump()
        # Filename is a display label only: it must exactly match the stored
        # filename of the single pinned local version. A renderer-controlled
        # label must never relabel a sensitive file as an ordinary name.
        stored = runtime.store.version(typed["inputs"][0]["ref"])
        if typed["filename"] != stored["filename"]:
            reject("input_changed", "filename must match the pinned local version")
        check_imported(typed["inputs"])

        def build(value, now):
            return center_file_scope(value, local_node_id=runtime.store.environment_id,
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
    @router.post("/{plan_id}/cancel")
    async def cancel(plan_id: str, body: CenterRequest, request: Request):
        # Operable cancel for file-compute: calls the center cancel through the
        # persisted remote_compute_id and saves the authoritative receipt. Works
        # even after local revoke (reconcile stays available) but never sends
        # original bytes again. Mandatory Idempotency-Key; transport failures
        # stay unknown and are raised, never mapped to cancelled.
        check_workspace(body.workspace if body else None)
        config = center_config(body.center if body else None, plan_id)
        from ddp_local.federation_dispatch import cancel_remote_compute
        try:
            return await cancel_remote_compute(
                runtime, plan_id, config, actor_headers=actor_headers(request),
                operation_key=operation_key(request))
        except CenterFault as exc:
            return center_fault(plan_id, exc)

    @router.post("/{plan_id}/transfer/authorize")
    async def transfer_authorize(plan_id: str, body: TransferAuthorize, request: Request):
        # Host-only fixed restricted transfer authorization, read as a query
        # (not a cached command): the host calls it before every upload/resume
        # action with a fresh Idempotency-Key, so a revoked/expired approval
        # can never reuse an old ticket. Never in the renderer whitelist.
        check_workspace(body.workspace)
        config = center_config(body.center, plan_id)
        from ddp_local.federation_dispatch import authorize_file_transfer
        return authorize_file_transfer(
            runtime, plan_id, config, action=body.action,
            operation_key=operation_key(request), upload_id=body.upload_id,
            offset=body.offset, length=body.length)

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
        # Read-only: refresh the persisted center mirror, never stage new edges.
        check_workspace(body.workspace if body else None)
        config = center_config(body.center if body else None, plan_id)
        try:
            return await runtime.federation_reconcile(plan_id, config, actor_headers=actor_headers(request))
        except CenterFault as exc:
            return center_fault(plan_id, exc)

    @router.post("/{plan_id}/resume", status_code=201)
    async def resume(plan_id: str, request: Request, body: CenterRequest | None = None):
        """Stage a fresh center ready revision for a terminal query plan.

        Same CenterRequest binding and error handling as reconcile: mandatory
        Idempotency-Key, reviewed-center credential resolution inside
        `runtime.federation_resume`, result is the ordinary persisted
        federation state. New approval followed by resume (not POST /tasks)
        is required to execute the staged revision; same root budget. Never
        automatically approved here: the caller must show, review and approve
        the staged revision.
        """
        check_workspace(body.workspace if body else None)
        config = center_config(body.center if body else None, plan_id)
        try:
            return await runtime.federation_resume(
                plan_id, config, operation_key=operation_key(request),
                actor_headers=actor_headers(request))
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
