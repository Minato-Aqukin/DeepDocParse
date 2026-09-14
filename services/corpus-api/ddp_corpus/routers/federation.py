"""P5 peer endpoints (`docs/refactor/P5-INTERFACES-v3.md` §2).

These routes are **not** proxied by control-api (`corpusPrefixes` does not list
`/api/v1/federation`): they are the node-to-node boundary. Authentication is
two-layered and fail closed:

1. the standard service credential + actor context every corpus route needs
   (`deps.current_actor`), and
2. `X-DDP-Peer-Token`, compared in constant time against
   `settings.federation_peer_token`. No configured token means 401
   `peer_unauthenticated` — a node without a registered trust credential must not
   execute anyone's work.

`X-DDP-Target-Node`, when present, must name this node (`wrong_target`). Bodies
carry the same identity for probes/admissions, checked there as well.
"""
from typing import Literal

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

import hmac

from ddp_corpus import federation
from ddp_corpus.config import settings
from ddp_corpus.db import get_session
from ddp_corpus.deps import Actor, current_actor
from ddp_corpus.errors import APIError
from ddp_corpus.models import utcnow

router = APIRouter(prefix="/api/v1/federation")


async def require_peer(x_ddp_peer_token: str | None = Header(default=None)) -> None:
    configured = settings.federation_peer_token or ""
    presented = x_ddp_peer_token or ""
    if not configured or not presented or not hmac.compare_digest(configured, presented):
        raise APIError(401, "invalid or missing peer credentials", "authentication_error",
                       "peer_unauthenticated")


async def require_target(x_ddp_target_node: str | None = Header(default=None)) -> None:
    if x_ddp_target_node and x_ddp_target_node != (settings.bundle_node_id or "").strip():
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
                       actor: Actor = Depends(current_actor),
                       _peer: None = Depends(require_peer),
                       _target: None = Depends(require_target),
                       session: AsyncSession = Depends(get_session),
                       idempotency_key: str = Header(min_length=1, max_length=128)):
    return await federation.run_probe(
        session, actor, body.model_dump(by_alias=True), now=utcnow(),
        http=_http(request), index=_index(request), idempotency_key=idempotency_key)


@router.get("/probes/{probe_id}")
async def get_probe(probe_id: str, actor: Actor = Depends(current_actor),
                    _peer: None = Depends(require_peer),
                    _target: None = Depends(require_target),
                    session: AsyncSession = Depends(get_session)):
    return await federation.get_probe(session, actor, probe_id, now=utcnow())


@router.post("/admissions", status_code=201)
async def create_admission(body: AdmissionRequest, request: Request,
                           actor: Actor = Depends(current_actor),
                           _peer: None = Depends(require_peer),
                           _target: None = Depends(require_target),
                           _enabled: None = Depends(require_admissions_enabled),
                           session: AsyncSession = Depends(get_session),
                           idempotency_key: str = Header(min_length=1, max_length=128)):
    if body.idempotency_key != idempotency_key:
        raise APIError(400, "Idempotency-Key header must match the request body",
                       "invalid_request_error", "idempotency_key_mismatch")
    receipt, created = await federation.admit(
        session, actor, body.model_dump(by_alias=True), now=utcnow(),
        http=_http(request), index=_index(request))
    return JSONResponse(receipt, status_code=201 if created else 200,
                        headers={"Cache-Control": "no-store"})


@router.post("/admissions/lookup")
async def lookup_admission(body: LookupRequest, actor: Actor = Depends(current_actor),
                           _peer: None = Depends(require_peer),
                           _target: None = Depends(require_target),
                           session: AsyncSession = Depends(get_session)):
    receipt = await federation.lookup_admission(session, actor, body.idempotency_key)
    return JSONResponse(receipt, headers={"Cache-Control": "no-store"})


@router.get("/tasks/{executor_task_id}")
async def get_execution(executor_task_id: str, actor: Actor = Depends(current_actor),
                        _peer: None = Depends(require_peer),
                        _target: None = Depends(require_target),
                        session: AsyncSession = Depends(get_session)):
    return await federation.get_execution(session, actor, executor_task_id)


@router.post("/tasks/{executor_task_id}/cancel")
async def cancel_execution(executor_task_id: str, actor: Actor = Depends(current_actor),
                           _peer: None = Depends(require_peer),
                           _target: None = Depends(require_target),
                           session: AsyncSession = Depends(get_session)):
    return await federation.cancel_execution(session, actor, executor_task_id, now=utcnow())


@router.post("/resources/locate")
async def locate_resource(body: LocateRequest, actor: Actor = Depends(current_actor),
                          _peer: None = Depends(require_peer),
                          _target: None = Depends(require_target),
                          session: AsyncSession = Depends(get_session)):
    return await federation.locate(session, actor, resource_id=body.resource_id,
                                   version_id=body.version_id)


@router.post("/results/resolve")
async def resolve_result(body: ResolveRequest, actor: Actor = Depends(current_actor),
                         _peer: None = Depends(require_peer),
                         _target: None = Depends(require_target),
                         session: AsyncSession = Depends(get_session)):
    return await federation.resolve_evidence(session, actor, evidence_ref=body.evidence_ref,
                                             now=utcnow())


@router.get("/evidence-sets/{set_ref}")
async def read_evidence_set(set_ref: str, actor: Actor = Depends(current_actor),
                            _peer: None = Depends(require_peer),
                            _target: None = Depends(require_target),
                            session: AsyncSession = Depends(get_session)):
    return await federation.read_evidence_set(session, actor, set_ref, now=utcnow())
