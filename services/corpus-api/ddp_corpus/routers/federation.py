"""P5 peer endpoints (`docs/refactor/P5-INTERFACES-v3.md` §2).

These routes are **not** proxied by control-api (`corpusPrefixes` does not list
`/api/v1/federation`): they are the node-to-node boundary. Authentication is a
single-use node credential (`ddp-node-credential/1`, see
`packages/contracts/ddp/node-credential-format.md` and `ddp_corpus/node_auth.py`):

- **no** `SERVICE_TOKEN`, **no** caller-asserted actor headers: the remote caller is
  derived from the verified credential as a read-only `peer-*` principal inside the
  local organization that approved the issuing node;
- every route pins its credential operation (`ROUTE_OPERATIONS`, the same table the
  contract declares as `x-ddp-node-credential-operation`);
- the credential's scope constraints are compared with the request body and with the
  row the route reads (`PeerContext.require` / `PeerContext.within`) — authentication
  is not authorization, and the local resource ACL still decides after all that.

`FEDERATION_PEER_AUTH=shared_token_insecure` keeps the old shared-token + actor-header
path for development fixtures only (startup refuses it without ALLOW_INSECURE_DEFAULTS).

`X-DDP-Target-Node`, when present, must name this node (`wrong_target`). Bodies
carry the same identity for probes/admissions, checked there as well.
"""
from typing import Literal

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ddp_corpus import catalog, federation, node_identity
from ddp_corpus.config import settings
from ddp_corpus.db import get_session
from ddp_corpus.deps import Actor
from ddp_corpus.errors import APIError, error_body
from ddp_corpus.federation_models import FederationProbe
from ddp_corpus.models import utcnow
from ddp_corpus.node_auth import PeerContext, peer_context

router = APIRouter(prefix="/api/v1/federation")

#: (method, route path) -> node credential operation. The contract declares the
#: same mapping per operation (`x-ddp-node-credential-operation`); a guard test
#: compares the two so neither side can drift.
ROUTE_OPERATIONS = {
    ("POST", "/api/v1/federation/probes"): "probe_create",
    ("GET", "/api/v1/federation/probes/{probe_id}"): "probe_read",
    ("POST", "/api/v1/federation/admissions"): "admission_create",
    ("POST", "/api/v1/federation/admissions/lookup"): "admission_lookup",
    ("GET", "/api/v1/federation/tasks/{executor_task_id}"): "execution_read",
    ("POST", "/api/v1/federation/tasks/{executor_task_id}/cancel"): "execution_cancel",
    ("POST", "/api/v1/federation/resources/locate"): "resource_locate",
    ("POST", "/api/v1/federation/results/resolve"): "result_resolve",
    ("GET", "/api/v1/federation/evidence-sets/{set_ref}"): "evidence_set_read",
    ("GET", "/api/v1/federation/published-collections"): "catalog_read",
}


def _peer(method: str, path: str):
    return Depends(peer_context(ROUTE_OPERATIONS[(method, path)]))


async def require_target(x_ddp_target_node: str | None = Header(default=None)) -> None:
    if x_ddp_target_node and x_ddp_target_node != node_identity.local_node_id():
        raise APIError(409, "request targets another node", "invalid_request_error",
                       "wrong_target")


async def require_admissions_enabled() -> None:
    if not settings.federation_admissions_enabled:
        raise APIError(503, "this node does not accept federated admissions",
                       "server_error", "admissions_disabled")


class ProbeRequest(BaseModel):
    """`federation-tasks-v1.yaml#ProbeRequest` 的冻结形状（additionalProperties=false）。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    schema_: Literal["ddp-task-probe/1#ProbeRequest"] = Field(
        default="ddp-task-probe/1#ProbeRequest", alias="schema")
    task_spec_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    consent_ref: str = Field(min_length=1, max_length=128)
    probe_kind: Literal["capability_input", "resource_locate", "evidence_retrieval"]
    target_node_id: str = Field(min_length=1, max_length=64)
    scope_ref: str | None = Field(default=None, min_length=1)
    collection_id: str | None = Field(default=None, min_length=1, max_length=32)
    query: str | None = Field(default=None, max_length=2000)
    query_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    candidate_limit: int | None = Field(default=None, ge=1, le=200)
    operation: str | None = Field(default=None, min_length=1, max_length=64)


class InputItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ref: str = Field(min_length=1, max_length=128)
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    size_bytes: int | None = Field(default=None, ge=0)


class EvidenceItem(BaseModel):
    """类型化 `evidence_excerpts` 数据边的一条载荷。

    Pydantic 这里只做防滥用的粗界（4096 字符/64 条），**契约的 2000/50 由
    执行者 `_verify_evidence` 把关并给 `input_not_verified` 机器码** —— 契约
    约束与业务拒绝必须是同一条可测路径，不能因为 Pydantic 先生效而变成另一
    种 422 形状。
    """

    model_config = ConfigDict(extra="forbid")
    evidence_id: str = Field(min_length=1, max_length=128)
    excerpt: str = Field(min_length=1, max_length=4096)
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class AdmissionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    schema_: Literal["ddp-plan-admission/1#AdmissionRequest"] = Field(
        default="ddp-plan-admission/1#AdmissionRequest", alias="schema")
    idempotency_key: str = Field(min_length=1, max_length=128)
    root_task_id: str = Field(min_length=1, max_length=64)
    step_id: str = Field(min_length=1, max_length=64)
    delegation_generation: int = Field(default=0, ge=0)
    task_spec: dict
    plan: dict
    execution_consent: dict
    inputs: list[InputItem] = Field(default_factory=list)
    evidence: list[EvidenceItem] = Field(default_factory=list, max_length=64)


class LookupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=1, max_length=128)


class LocateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resource_id: str = Field(min_length=1, max_length=32)
    version_id: str | None = Field(default=None, max_length=32)


class ResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evidence_ref: str = Field(min_length=1, max_length=128)


def _index(request: Request):
    return getattr(request.app.state, "search_index", None)


def _http(request: Request):
    return getattr(request.app.state, "http", None)


@router.post("/probes", status_code=201)
async def create_probe(body: ProbeRequest, request: Request,
                       peer: PeerContext = _peer("POST", "/api/v1/federation/probes"),
                       _target: None = Depends(require_target),
                       session: AsyncSession = Depends(get_session),
                       idempotency_key: str = Header(min_length=1, max_length=128)):
    # 探测凭证钉在一份需求修订与一个范围上：请求体换了需求或范围，同一张凭证不覆盖。
    peer.require(task_spec_digest=body.task_spec_digest, scope_ref=body.scope_ref)
    return await federation.run_probe(
        session, peer.actor, body.model_dump(by_alias=True), now=utcnow(),
        http=_http(request), index=_index(request), idempotency_key=idempotency_key)


@router.get("/probes/{probe_id}")
async def get_probe(probe_id: str,
                    peer: PeerContext = _peer("GET", "/api/v1/federation/probes/{probe_id}"),
                    _target: None = Depends(require_target),
                    session: AsyncSession = Depends(get_session)):
    result = await federation.get_probe(session, peer.actor, probe_id, now=utcnow())
    peer.require(task_spec_digest=(result or {}).get("task_spec_digest"))
    return result


@router.post("/admissions", status_code=201)
async def create_admission(body: AdmissionRequest, request: Request,
                           peer: PeerContext = _peer("POST", "/api/v1/federation/admissions"),
                           _target: None = Depends(require_target),
                           _enabled: None = Depends(require_admissions_enabled),
                           session: AsyncSession = Depends(get_session),
                           idempotency_key: str = Header(min_length=1, max_length=128)):
    if body.idempotency_key != idempotency_key:
        raise APIError(400, "Idempotency-Key header must match the request body",
                       "invalid_request_error", "idempotency_key_mismatch")
    peer.require(root_task_id=body.root_task_id, step_id=body.step_id)
    if peer.claims is not None and body.plan.get("root_coordinator_node_id") != peer.issuer_node_id:
        # 越权委托：一个节点不能以另一个协调者的名义提交计划。计划里写着谁在协调，
        # 签名证明的是谁在发 —— 两者必须是同一个节点。
        raise APIError(403, "only the plan's root coordinator may submit its admissions",
                       "permission_error", "credential_scope_denied")
    receipt, created = await federation.admit(
        session, peer.actor, body.model_dump(by_alias=True), now=utcnow(),
        http=_http(request), index=_index(request))
    return JSONResponse(receipt, status_code=201 if created else 200,
                        headers={"Cache-Control": "no-store"})


@router.post("/admissions/lookup")
async def lookup_admission(body: LookupRequest,
                           peer: PeerContext = _peer("POST",
                                                     "/api/v1/federation/admissions/lookup"),
                           _target: None = Depends(require_target),
                           session: AsyncSession = Depends(get_session)):
    receipt = await federation.lookup_admission(session, peer.actor, body.idempotency_key)
    peer.within(root_task_id=receipt.get("root_task_id"), step_id=receipt.get("step_id"))
    return JSONResponse(receipt, headers={"Cache-Control": "no-store"})


@router.get("/tasks/{executor_task_id}")
async def get_execution(executor_task_id: str,
                        peer: PeerContext = _peer("GET",
                                                  "/api/v1/federation/tasks/{executor_task_id}"),
                        _target: None = Depends(require_target),
                        session: AsyncSession = Depends(get_session)):
    row = await federation.require_execution(session, peer.actor, executor_task_id)
    peer.within(root_task_id=row.root_task_id, step_id=row.step_id)
    return federation.execution_status(row)


@router.post("/tasks/{executor_task_id}/cancel")
async def cancel_execution(executor_task_id: str,
                           peer: PeerContext = _peer(
                               "POST", "/api/v1/federation/tasks/{executor_task_id}/cancel"),
                           _target: None = Depends(require_target),
                           session: AsyncSession = Depends(get_session)):
    # 先判范围再取消：一张为别的根任务签的凭证不许取消这一条执行。
    row = await federation.require_execution(session, peer.actor, executor_task_id)
    peer.within(root_task_id=row.root_task_id, step_id=row.step_id)
    return await federation.cancel_execution(session, peer.actor, executor_task_id, now=utcnow())


@router.post("/resources/locate")
async def locate_resource(body: LocateRequest,
                          peer: PeerContext = _peer("POST", "/api/v1/federation/resources/locate"),
                          _target: None = Depends(require_target),
                          session: AsyncSession = Depends(get_session)):
    return await federation.locate(session, peer.actor, resource_id=body.resource_id,
                                   version_id=body.version_id)


@router.post("/results/resolve")
async def resolve_result(body: ResolveRequest,
                         peer: PeerContext = _peer("POST", "/api/v1/federation/results/resolve"),
                         _target: None = Depends(require_target),
                         session: AsyncSession = Depends(get_session)):
    return await federation.resolve_evidence(session, peer.actor, evidence_ref=body.evidence_ref,
                                             now=utcnow())


@router.get("/evidence-sets/{set_ref}")
async def read_evidence_set(set_ref: str,
                            peer: PeerContext = _peer("GET",
                                                      "/api/v1/federation/evidence-sets/{set_ref}"),
                            _target: None = Depends(require_target),
                            session: AsyncSession = Depends(get_session)):
    if peer.claims is not None:
        if set_ref.startswith("federation-execution:"):
            row = await federation.require_execution(session, peer.actor,
                                                     set_ref.split(":", 1)[1])
            peer.within(root_task_id=row.root_task_id, step_id=row.step_id)
        elif set_ref.startswith("federation-probe:"):
            probe = await session.get(FederationProbe, set_ref.split(":", 1)[1])
            if probe is not None and probe.organization_id == peer.actor.organization_id \
                    and probe.actor_id == federation.acting_actor(peer.actor):
                # 探测证据集没有根任务，钉的是需求修订：凭证必须带着它并且相等。
                peer.require(task_spec_digest=probe.task_spec_digest)
    return await federation.read_evidence_set(session, peer.actor, set_ref, now=utcnow())


@router.get("/published-collections")
async def read_published_collections(
        snapshot_id: str = Query(default="", max_length=32),
        cursor: str = Query(default="", max_length=32),
        limit: int | None = Query(default=None, ge=1, le=100),
        peer: PeerContext = _peer("GET", "/api/v1/federation/published-collections"),
        _target: None = Depends(require_target),
        session: AsyncSession = Depends(get_session)):
    """对等目录读：与控制面代理的 `/api/v1/federation/collections` 同一个生产者。

    生产者只接受服务身份，范围与调用者摘要在服务端固定；这里给它的是**批准该
    节点的本地组织**里的一个服务视角，调用者选不了看谁的目录。
    """
    service = Actor(id=peer.actor.id, kind="service", organization_id=peer.actor.organization_id,
                    role="viewer", request_id=peer.actor.request_id)
    try:
        page = await catalog.published_snapshot_page(session, service, snapshot_id, cursor, limit)
    except APIError as exc:
        revoked = getattr(exc, "revoked_collection_ids", None)
        if revoked is not None:
            return JSONResponse(jsonable_encoder({**error_body(str(exc.detail), exc.type, exc.code),
                                                  "revoked_collection_ids": revoked}),
                                status_code=410, headers={"Cache-Control": "no-store"})
        raise
    return JSONResponse(jsonable_encoder(page), headers={"Cache-Control": "no-store"})
