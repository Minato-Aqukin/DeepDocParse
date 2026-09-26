"""Federated Wiki generation and commit (coordinator calls, owner persists).

Two functions, no routes here:

- `generate_federated` turns foreign `ddp-evidence/1#FederatedEvidence`
  envelopes (with their bounded `excerpt`) into a real model draft via the
  shared kernel `ddp_core.application.wiki.generate_wiki`. The model provider
  is the actually configured chat endpoint (`settings.chat_*` through
  `ddp_corpus.upstream.chat_request`) — never the RAG answer path, and the
  produced pages are never fed back as original evidence.
- `commit_federated_revision` persists that draft with the existing versioned
  Wiki machinery (`wiki.append_revision` CAS + `WikiWriteKey` + `revision_out`).
  Foreign evidence is stored verbatim in `wiki_dependencies` (origin /
  authority / true source evidence id / publication / policy revision / grant /
  receipt) and is never rewritten into a local `source_version_id`
  foreign-key impersonation.
- `source_ref(envelope)` is the stable internal evidence reference:
  `source:+sha256(canonical(origin, resource, version, parse, evidence_id))`.

Coordinator contract (Main-frozen, agreed with CoordinatorImplement):

- Evidence items are `{evidence_id=<ref>, source_envelope={<true envelope>},
  excerpt}` where `<ref> == source_ref(source_envelope)` (verified here; a
  mismatch is a forged binding). `source_envelope.evidence_id` keeps the true
  source id; kernel rows, claims and relations bind through the ref while the
  dependency manifest keeps both (`evidence_id=<ref>`,
  `source_evidence_id=<true id>`). Same true id on different origins never
  collides and is never merged.
- `generate_federated(http, *, body, evidence, max_tokens)` where `body` is
  `{title, max_pages?, wiki_id?, base_revision_id?}`.
- `commit_federated_revision(session, actor, *, root_task_id, task_spec,
  result, evidence)` returns the existing `revision_out` shape; the
  coordinator stores only `result.wiki = {wiki_id, revision_id}` and reads
  everything else through the versioned Wiki API.
"""
from __future__ import annotations

import copy
import hashlib

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ddp_core.application import plans as plans_kernel
from ddp_core.application import wiki as wiki_kernel
from ddp_core.application.ports import ApplicationError
from ddp_core.bundle import NODE_ID as _NODE_ID_PATTERN
from ddp_core.bundle import json_bytes
from ddp_corpus import node_identity as node_identity_plane
from ddp_corpus import wiki as wiki_plane
from ddp_corpus.config import settings
from ddp_corpus.deps import Actor
from ddp_corpus.errors import APIError
from ddp_corpus.models import Wiki
from ddp_corpus.upstream import chat_request


def _node_ok(value) -> bool:
    return isinstance(value, str) and bool(_NODE_ID_PATTERN.fullmatch(value))


#: Federated evidence-set bound, same ruler as the admission path
#: (`federation._verify_evidence` / `ADMISSION_EVIDENCE_LIMIT`) and the
#: evidence-set read (`EVIDENCE_SET_LIMIT`). Over is an explicit error,
#: never a silent truncation.
EVIDENCE_LIMIT = 50
EXCERPT_CHARS = 2000
INPUT_CHARS = 50000
RESPONSE_BYTES = 512_000

_LOCATOR_KINDS = ("page_block", "table_cell", "paragraph")


def _fail(status: int, code: str, message: str):
    raise APIError(status, message, "invalid_request_error", code)


def _status_for_kernel(code: str) -> int:
    if code in ("wiki_generation_invalid", "wiki_generation_failed"):
        return 502
    if code in ("wiki_source_permission", "remote_execution_denied"):
        return 403
    return 409


def _strip_digest(value, *, field: str) -> str:
    # Bundle/federation envelopes carry `sha256:<hex>`; the wiki dependency
    # ledger stores the bare hex. Strip exactly one prefix, then validate shape
    # here so a malformed digest fails closed instead of persisting.
    text = value[7:] if isinstance(value, str) and value.startswith("sha256:") else value
    if not isinstance(text, str) or len(text) != 64 or any(
        char not in "0123456789abcdef" for char in text
    ):
        _fail(400, "wiki_source_unavailable", f"federated {field} is missing")
    return text


def source_ref(envelope: dict) -> str:
    """Stable internal evidence reference for one true source envelope.

    `source:+sha256(canonical(origin, resource_id, source_version_id,
    parse_revision, evidence_id))` — two origins sharing an `evidence_id`
    never collide; a relay cannot forge a different source onto the same ref
    because every tuple field comes from the verified envelope. Always <= 128
    chars (7 + 64 hex), never truncated.
    """
    if not isinstance(envelope, dict):
        _fail(400, "wiki_source_unavailable", "federated envelope must be an object")
    parts = [envelope.get(key) for key in (
        "origin_node_id", "resource_id", "source_version_id", "parse_revision", "evidence_id")]
    if any(not isinstance(part, str) or not part for part in parts):
        _fail(400, "wiki_source_unavailable", "federated envelope identity is incomplete")
    raw = json_bytes(list(parts))
    return "source:+" + hashlib.sha256(raw).hexdigest()


def _split_item(item: dict) -> tuple[dict, str | None, dict]:
    """Accept coordinator `{evidence_id=ref, source_envelope={...}, excerpt}`
    as well as legacy `{envelope..., excerpt}` / `{evidence: {...}, excerpt}`.

    Coordinator contract (Main-frozen): C never rewrites the origin; the wiki
    payload `evidence_id` carries the `source_ref(envelope)` ref while
    `source_envelope.evidence_id` keeps the true source id. The helper rebinds
    them here and validates the ref matches `source_ref(source_envelope)` —
    a mismatch is a forged binding, never silently repaired.
    """
    if not isinstance(item, dict):
        return {}, None, {}
    source_envelope = item.get("source_envelope")
    if isinstance(source_envelope, dict):
        envelope = dict(source_envelope)
        excerpt = item.get("excerpt")
        extra = {k: v for k, v in item.items()
                 if k not in ("source_envelope", "excerpt") and not str(k).startswith("_")}
        for key, value in extra.items():
            envelope.setdefault(key, value)
        claimed = item.get("evidence_id")
        if isinstance(claimed, str) and claimed:
            envelope["_claimed_ref"] = claimed
        return envelope, excerpt, extra
    nested = item.get("evidence")
    if isinstance(nested, dict) and isinstance(item.get("excerpt"), str):
        envelope = dict(nested)
        excerpt = item.get("excerpt")
        extra = {k: v for k, v in item.items() if k not in ("evidence", "excerpt")}
        for key, value in extra.items():
            envelope.setdefault(key, value)
        return envelope, excerpt, extra
    envelope = {k: v for k, v in item.items()
                if k != "excerpt" and not str(k).startswith("_")}
    return envelope, item.get("excerpt"), {}


def _validate_item(item: dict) -> tuple[dict, dict]:
    """Strict envelope+excerpt binding; returns (kernel_row, dep_meta).

    The original origin/resource/version/true-evidence/page/bbox envelope is
    kept verbatim in `dep_meta` (locator, origin, authority, source ids);
    nothing is re-invented, and a generated page can never pass as original.
    """

    envelope, excerpt, _ = _split_item(item)
    evidence_id = envelope.get("evidence_id")
    if not isinstance(evidence_id, str) or not 1 <= len(evidence_id) <= 128:
        _fail(400, "wiki_source_unavailable", "federated evidence_id is missing")
    ref = source_ref({key: envelope.get(key) for key in (
        "origin_node_id", "resource_id", "source_version_id", "parse_revision", "evidence_id")})
    claimed = envelope.pop("_claimed_ref", None)
    if claimed is not None and claimed != ref:
        _fail(409, "wiki_source_unavailable", "federated evidence ref does not match its source envelope")
    source_evidence_id = evidence_id
    for key in ("origin_node_id", "authority_node_id"):
        if not _node_ok(envelope.get(key)):
            _fail(400, "wiki_source_unavailable", f"federated {key} is not a node identity")
    for key in ("resource_id", "source_version_id", "parse_revision"):
        value = envelope.get(key)
        if not isinstance(value, str) or not 1 <= len(value) <= 128:
            _fail(400, "wiki_source_unavailable", f"federated {key} is missing")
    source_digest = _strip_digest(envelope.get("source_digest"), field="source_digest")
    excerpt_digest = _strip_digest(envelope.get("excerpt_digest"), field="excerpt_digest")
    locator = envelope.get("locator")
    if (not isinstance(locator, dict) or locator.get("kind") not in _LOCATOR_KINDS
            or type(locator.get("physical_page_index")) is not int
            or locator["physical_page_index"] < 0
            or type(locator.get("seq")) is not int or locator["seq"] < 0):
        _fail(409, "wiki_source_unavailable", "federated locator is not a fixed original position")
    bbox = locator.get("bbox")
    if bbox is not None:
        page_size = locator.get("page_size")
        if (not isinstance(bbox, list) or len(bbox) != 4
                or not all(isinstance(n, (int, float)) for n in bbox)
                or not isinstance(page_size, dict)
                or not isinstance(page_size.get("width"), (int, float))
                or not isinstance(page_size.get("height"), (int, float))):
            _fail(409, "wiki_source_unavailable", "federated bbox needs its page_size companion")
    if envelope.get("source_type") != "source" or envelope.get("derived_from") is not None:
        _fail(409, "wiki_source_unavailable", "generated pages cannot serve as original sources")
    policy_revision = envelope.get("policy_revision")
    if not isinstance(policy_revision, str) or not policy_revision:
        _fail(409, "wiki_source_unavailable", "federated policy_revision is missing")
    if len(policy_revision) > 128:
        _fail(400, "wiki_source_unavailable", "federated policy_revision exceeds its bound")
    reason = _excerpt_reason(excerpt)
    if reason is not None:
        _fail(409, "wiki_source_unavailable", f"federated excerpt {reason}")
    actual = "sha256:" + hashlib.sha256(excerpt.encode("utf-8")).hexdigest()
    if envelope.get("excerpt_digest") != actual:
        _fail(409, "wiki_source_unavailable", "federated excerpt differs from its fixed digest")
    publication = envelope.get("source_publication")
    if not isinstance(publication, str) or not publication:
        publication = policy_revision.split(":", 1)[0] if ":" in policy_revision else policy_revision
    grant = envelope.get("derivative_grant")
    if publication in ("private", "withdrawn") and not (isinstance(grant, str) and grant):
        _fail(403, "wiki_source_permission",
              "private federated source needs an explicit derivative grant")
    if publication not in ("published", "private"):
        # `unmapped` and anything unrecognised fail closed: inventing a
        # publication would be faking the authorization this row claims.
        _fail(409, "wiki_source_unavailable", "federated source publication is not verifiable")
    receipt = envelope.get("retrieval_receipt_ref")
    if receipt is not None and (not isinstance(receipt, str) or len(receipt) > 128):
        _fail(400, "wiki_source_unavailable", "federated retrieval receipt exceeds its bound")
    if isinstance(grant, str) and len(grant) > 128:
        _fail(400, "wiki_source_unavailable", "federated derivative grant exceeds its bound")
    public_envelope = dict(envelope)
    public_envelope.pop("excerpt", None)
    kernel_row = {"id": ref, "evidence": public_envelope, "excerpt": excerpt}
    meta = {
        "resource_id": str(envelope["resource_id"]),
        "source_version_id": str(envelope["source_version_id"]),
        "document_id": "",
        "source_digest": source_digest,
        "parse_revision": str(envelope["parse_revision"]),
        "evidence_id": ref,
        "source_evidence_id": source_evidence_id,
        "excerpt_digest": excerpt_digest,
        "locator": copy.deepcopy(locator),
        "origin_node_id": str(envelope["origin_node_id"]),
        "authority_node_id": str(envelope["authority_node_id"]),
        "source_publication": str(publication),
        "policy_revision": str(policy_revision),
        "derivative_grant": str(grant) if isinstance(grant, str) and grant else None,
        "retrieval_receipt_ref": str(receipt) if isinstance(receipt, str) and receipt else None,
    }
    return kernel_row, meta


async def _resolve_local_binding(session, actor, meta: dict) -> dict:
    """Resolve a mirrored-local meta to its real local bindings.

    The envelope's claimed `resource_id`/`source_version_id`/`document_id`
    are NOT trusted: this re-resolves the true local ResourceVersion +
    Document + Evidence by the bound ref identity and returns the real
    bindings. A local claim that does not resolve to a live local row fails
    closed (`missing` -> caller 404s the whole commit). Foreign rows never
    reach here — the caller only resolves `origin == local node`.
    """
    from ddp_corpus.models import Document, Evidence, ResourceVersion
    from ddp_corpus.policy import require_resource
    resource = await require_resource(session, actor, meta["resource_id"])
    version = await session.get(ResourceVersion, meta["source_version_id"])
    document = await session.get(Document, meta["document_id"]) if meta.get("document_id") else None
    evidence = await session.get(Evidence, meta["source_evidence_id"] or meta["evidence_id"])
    if (version is None or version.resource_id != resource.id
            or evidence is None or evidence.derived_from is not None):
        _fail(404, "wiki_source_unavailable", "local federated source is no longer available")
    if document is None or document.id != evidence.document_id:
        document = await session.get(Document, evidence.document_id)
    if document is None or document.deleted_at is not None:
        _fail(404, "wiki_source_unavailable", "local federated source is no longer available")
    return {"resource_id": resource.id, "source_version_id": version.id,
            "document_id": document.id, "source_digest": version.source_digest,
            "parse_revision": version.parse_job_id or evidence.parse_job_id}

def _excerpt_reason(text) -> str | None:
    if not isinstance(text, str) or not text.strip():
        return "unavailable"
    if len(text) > EXCERPT_CHARS:
        return "over_contract_bound"
    return None


class _ChatProvider:
    """The actually configured chat endpoint as an `ExecutionProvider`.

    Only `settings.chat_endpoint` is ever contacted — there is no remote
    fallback and no per-evidence URL. `execution_policy` / `allow_remote`
    travel into the provenance for audit, they never pick a destination.
    """

    def __init__(self, http):
        self._http = http

    def capabilities(self) -> dict:
        return {"chat_endpoint": settings.chat_endpoint,
                "model": settings.chat_model or "registry-default"}

    async def generate(self, messages, *, execution_policy, allow_remote, max_tokens=1024):
        if type(max_tokens) is not int or not 1 <= max_tokens <= 8192:
            raise APIError(409, "completion token budget must be within 1..8192",
                           "invalid_request_error", "wiki_budget_exceeded")
        request = chat_request(self._http, messages, stream=False)
        try:
            payload = __import__("json").loads(request.content)
        except ValueError:
            _fail(502, "wiki_generation_failed", "Wiki model request could not be built")
        payload["max_tokens"] = max_tokens
        built = self._http.build_request("POST", request.url, json=payload,
                                         headers=request.headers,
                                         extensions=request.extensions)
        built.headers["Content-Length"] = str(len(built.content))
        try:
            async with self._http.stream(built.method, built.url, content=built.content,
                                         headers=built.headers,
                                         extensions=built.extensions) as response:
                if response.status_code != 200:
                    _fail(502, "wiki_generation_failed", "Wiki model request failed")
                chunks, total = [], 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > RESPONSE_BYTES:
                        _fail(502, "wiki_budget_exceeded", "model response exceeded byte limit")
                    chunks.append(chunk)
        except APIError:
            raise
        except Exception as exc:  # noqa: BLE001 -- transport failure is a 502, never a draft.
            raise APIError(502, f"Wiki model request failed: {type(exc).__name__}",
                           "invalid_request_error", "wiki_generation_failed") from exc
        try:
            import json as _json
            body = _json.loads(b"".join(chunks))
            choice = body["choices"][0]
            if choice.get("finish_reason") == "length":
                _fail(409, "wiki_budget_exceeded", "model exhausted completion token budget")
            if body.get("usage", {}).get("completion_tokens", 0) > max_tokens:
                _fail(409, "wiki_budget_exceeded", "model exceeded completion token budget")
            output = choice["message"]["content"]
            if not isinstance(output, str) or not output.strip():
                raise ValueError("empty output")
        except APIError:
            raise
        except (KeyError, IndexError, TypeError, ValueError):
            _fail(502, "wiki_generation_failed", "Wiki model returned invalid output")
        model = body.get("model") if isinstance(body, dict) else None
        provenance = {"model": str(model or settings.chat_model or "registry-default"),
                      "kind": "federated_wiki_generation",
                      "endpoint": settings.chat_endpoint,
                      "execution_policy": execution_policy, "allow_remote": allow_remote}
        return output, provenance


def _recorder(entries: list):
    def record_attempt(stage, messages, allowance):
        entry = {"stage": stage, "allowance": allowance, "messages": messages}
        entries.append(entry)

        def finish(*, output=None, provider=None, error=None):
            entry.update(output=output, provider=provider, error=error)

        return finish

    return record_attempt


async def generate_federated(http, *, body: dict, evidence: list[dict], max_tokens: int) -> dict:
    """Draft federated Wiki pages from fixed foreign evidence.

    Returns the shared-kernel result (`pages`, `relations`, `provider`,
    `limits`, ...). The pages are a draft, not original evidence — the
    caller must persist them through `commit_federated_revision`, which
    re-binds every claim to the fixed envelopes below.
    """
    if not isinstance(body, dict):
        _fail(400, "wiki_title_invalid", "federated Wiki body must be an object")
    title = body.get("title")
    if not isinstance(title, str) or not title.strip() or len(title) > 255:
        _fail(400, "wiki_title_invalid", "federated Wiki needs a title within 1..255 characters")
    max_pages = body.get("max_pages", 4)
    if type(max_pages) is not int or not 1 <= max_pages <= 12:
        _fail(409, "wiki_budget_exceeded", "federated max_pages must be within 1..12")
    if type(max_tokens) is not int or not 512 <= max_tokens <= 8192:
        _fail(409, "wiki_budget_exceeded", "federated max_tokens must be within 512..8192")
    if not isinstance(evidence, list) or not 1 <= len(evidence) <= EVIDENCE_LIMIT:
        _fail(409, "wiki_budget_exceeded",
              f"federated evidence must hold 1..{EVIDENCE_LIMIT} fixed items")
    normalized, seen = [], set()
    for item in evidence:
        row, _ignored_meta = _validate_item(item)
        if row["id"] in seen:
            _fail(409, "wiki_source_unavailable", "federated evidence identities must be unique")
        seen.add(row["id"])
        normalized.append(row)
    try:
        limits = wiki_kernel.limits_for({
            "max_pages": max_pages, "max_evidence": len(normalized),
            "max_output_tokens": max_tokens, "max_input_chars": INPUT_CHARS})
    except ApplicationError as exc:
        raise APIError(_status_for_kernel(exc.code), str(exc),
                       "invalid_request_error", exc.code) from None
    if len(json_bytes(normalized).decode()) > INPUT_CHARS:
        _fail(409, "wiki_budget_exceeded", "federated original input exceeds its context budget")
    provider = _ChatProvider(http)
    attempts: list = []
    try:
        return await wiki_kernel.generate_wiki(
            provider, title.strip(), normalized, limits,
            execution_policy="trusted_federation", allow_remote=False,
            record_attempt=_recorder(attempts))
    except APIError:
        raise
    except ApplicationError as exc:
        raise APIError(_status_for_kernel(exc.code), str(exc),
                       "invalid_request_error", exc.code) from None


def _validate_result_pages(result: dict, *, max_pages: int, known: set[str]) -> list[dict]:
    pages = result.get("pages")
    if not isinstance(pages, list) or not 1 <= len(pages) <= max_pages:
        _fail(409, "wiki_generation_invalid", "federated result holds an invalid page count")
    keys, out = set(), []
    for page in pages:
        if not isinstance(page, dict):
            _fail(409, "wiki_generation_invalid", "federated page must be an object")
        key = page.get("page_key")
        title = page.get("title")
        sections = page.get("generated_sections")
        if (not isinstance(key, str) or not key or len(key) > 64 or key in keys
                or not isinstance(title, str) or not title.strip() or len(title) > 255
                or not isinstance(sections, list) or not sections):
            _fail(409, "wiki_generation_invalid", "federated page is malformed")
        keys.add(key)
        if not isinstance(page.get("human_paragraphs", []), list):
            _fail(409, "wiki_generation_invalid", "federated human paragraphs are malformed")
        for section in sections:
            if not isinstance(section, dict) or not isinstance(section.get("sentences"), list):
                _fail(409, "wiki_generation_invalid", "federated section is malformed")
            for claim in section["sentences"]:
                if not isinstance(claim, dict):
                    _fail(409, "wiki_generation_invalid", "federated claim must be an object")
                text = claim.get("text")
                refs = claim.get("evidence_ids")
                if (not isinstance(text, str) or not text.strip() or len(text) > 10000
                        or not isinstance(refs, list) or not refs
                        or any(not isinstance(r, str) for r in refs)
                        or not set(refs) <= known):
                    _fail(409, "unsupported_generation",
                          "federated claim cites unknown or missing original evidence")
        out.append(copy.deepcopy(page))
    return out


def _kernel_revalidate(pages: list[dict], relations, evidence: list[dict],
                       provider: dict) -> tuple[list[dict], list[dict]]:
    """Re-validate C-supplied structure through the shared Wiki kernel.

    C's claims arrive as `{text, evidence_ids: [ref...]}` without kernel `id`s
    and without kernel trust. Every ref must already be a `source_ref`
    bound above; here each claim/relation is re-checked (text bounds, ref
    membership, relation phrase-in-excerpt) and rewritten into canonical
    kernel rows (`wiki_sentence` ids, `edge_result` edges). Anything C
    invented or misbound fails closed — nothing of C's JSON is persisted
    verbatim.
    """
    by_id = {row["id"]: row for row in evidence}
    indexed = list(evidence)
    position_of = {row["id"]: n + 1 for n, row in enumerate(indexed)}
    raw_pages = []
    for number, page in enumerate(pages, 1):
        sections = []
        for section in page.get("generated_sections") or []:
            sentences = []
            for claim in section.get("sentences") or []:
                refs = claim.get("evidence_ids") or []
                numbers = []
                for ref in refs:
                    if ref not in by_id:
                        _fail(409, "unsupported_generation",
                              "federated claim cites unknown original evidence")
                    numbers.append(position_of[ref])
                sentences.append({"text": claim.get("text"), "references": numbers})
            sections.append({"heading": section.get("heading"), "sentences": sentences})
        raw_pages.append({"page": number, "sections": sections})
    plan = [{"page_key": page["page_key"], "title": page["title"],
             "references": [1], "source_term": None} for page in pages]
    raw_relations = []
    for item in relations or []:
        if not isinstance(item, dict):
            _fail(409, "wiki_generation_invalid", "federated relation must be an object")
        left = item.get("subject_page")
        right = item.get("object_page")
        refs = item.get("evidence_ids") or item.get("references") or []
        if isinstance(refs, list) and refs and all(isinstance(r, str) for r in refs):
            numbers = []
            for ref in refs:
                if ref not in by_id:
                    _fail(409, "unsupported_generation",
                          "federated relation cites unknown original evidence")
                numbers.append(position_of[ref])
        else:
            numbers = refs
        raw_relations.append({"subject_page": left, "object_page": right,
                              "predicate": item.get("predicate"), "references": numbers})
    try:
        clean_pages, edges = wiki_kernel.normalize_pages(
            {"pages": raw_pages, "relations": raw_relations}, plan, indexed, provider)
    except ApplicationError as exc:
        raise APIError(_status_for_kernel(exc.code), str(exc),
                       "invalid_request_error", exc.code) from None
    return clean_pages, edges


async def commit_federated_revision(session: AsyncSession, actor: Actor, *, root_task_id: str,
                                    task_spec: dict, result: dict, evidence: list[dict]) -> dict:
    """Persist a federated draft; returns the existing `revision_out` shape.

    Idempotency: `federated-wiki:{root_task_id}` under the caller's
    `(organization_id, actor_id)`. The same root never commits twice; a
    different request under the same key is a 409, and updating a Wiki whose
    current revision moved past `base_revision_id` is an explicit 409
    `revision_conflict`. Human paragraphs from the base revision are carried
    verbatim — the model never overwrites them.
    """
    import re as _re
    if (not isinstance(root_task_id, str)
            or not _re.fullmatch(r"[A-Za-z0-9_-]{1,64}", root_task_id)):
        _fail(400, "invalid_root_task", "root_task_id must be within [A-Za-z0-9_-]{1,64}")
    try:
        plans_kernel.validate_spec(task_spec)
    except ApplicationError as exc:
        raise APIError(_status_for_kernel(exc.code), str(exc),
                       "invalid_request_error", exc.code) from None
    if not isinstance(task_spec, dict) or task_spec.get("operation") != "wiki.pages":
        _fail(400, "capability_unsupported", "federated Wiki commits only serve wiki.pages")
    requirements = task_spec.get("requirements") or {}
    wiki_req = requirements.get("wiki") or {}
    if not isinstance(wiki_req, dict):
        _fail(400, "wiki_title_invalid", "federated Wiki needs requirements.wiki")
    actor.require(actor.can_upload and actor.principal_id is not None, "编写 Wiki")
    if not isinstance(result, dict):
        _fail(409, "wiki_generation_invalid", "federated result must be an object")
    if not isinstance(evidence, list) or not 1 <= len(evidence) <= EVIDENCE_LIMIT:
        _fail(409, "wiki_budget_exceeded",
              f"federated evidence must hold 1..{EVIDENCE_LIMIT} fixed items")
    normalized, metas, seen = [], [], set()
    for item in evidence:
        row, meta = _validate_item(item)
        if row["id"] in seen:
            _fail(409, "wiki_source_unavailable", "federated evidence identities must be unique")
        seen.add(row["id"])
        normalized.append(row)
        metas.append(meta)
    max_pages = wiki_req.get("max_pages", (result.get("limits") or {}).get("max_pages", 4))
    if type(max_pages) is not int or not 1 <= max_pages <= 12:
        _fail(409, "wiki_budget_exceeded", "federated max_pages must be within 1..12")
    pages = _validate_result_pages(result, max_pages=max_pages, known=seen)
    provider = dict((result.get("provider") or {}))
    relations_in = result.get("relations") or []
    if not isinstance(relations_in, list) or len(relations_in) > 50:
        _fail(409, "wiki_generation_invalid", "federated relations are malformed")
    key = f"federated-wiki:{root_task_id}"
    evidence_keys = sorted((m["origin_node_id"], m["resource_id"],
                            m["source_version_id"], m["parse_revision"],
                            m["evidence_id"], m["source_evidence_id"],
                            m["policy_revision"], m["excerpt_digest"]) for m in metas)
    result_digest = plans_kernel.digest(
        {"pages": pages, "relations": relations_in, "provider": provider,
         "limits": result.get("limits") or {}, "merge_conflicts": result.get("merge_conflicts") or []})
    request = {"operation": "federated_commit", "root_task_id": root_task_id,
               "task_spec": task_spec, "evidence_keys": evidence_keys,
               "result_digest": result_digest}
    previous = await wiki_plane.replay(session, actor, key, request)
    if previous:
        return previous
    wiki_id = wiki_req.get("wiki_id")
    base_revision_id = wiki_req.get("base_revision_id")
    if wiki_id is None:
        title = wiki_req.get("title")
        if not isinstance(title, str) or not title.strip():
            _fail(400, "wiki_title_invalid", "a new federated Wiki needs requirements.wiki.title")
        wiki = Wiki(organization_id=actor.organization_id, owner_id=actor.principal_id,
                    title=title.strip())
        base_id, old_pages, old_deps = None, [], []
        final_title = title.strip()
    else:
        if not isinstance(wiki_id, str) or not wiki_id:
            _fail(400, "wiki_title_invalid", "requirements.wiki.wiki_id is invalid")
        if not isinstance(base_revision_id, str) or not base_revision_id:
            _fail(409, "revision_conflict", "federated Wiki update needs base_revision_id")
        wiki = await wiki_plane.get_wiki(session, actor, wiki_id, write=True)
        if wiki.current_revision_id != base_revision_id:
            _fail(409, "revision_conflict", "Wiki changed; reload before saving")
        base_id = base_revision_id
        _revision, rows, old_deps = await wiki_plane.revision_data(session, base_id)
        if _revision is None or _revision.wiki_id != wiki.id:
            _fail(404, "not_found", "Wiki revision not found")
        await wiki_plane.dependency_state(session, actor, wiki, old_deps)
        old_pages = [{"page_key": row.page_key, "title": row.title,
                      "generated_sections": copy.deepcopy(row.generated_sections),
                      "human_paragraphs": copy.deepcopy(row.human_paragraphs)} for row in rows]
        title_req = wiki_req.get("title")
        final_title = title_req.strip() if isinstance(title_req, str) and title_req.strip() \
            else _revision.title
    for page in pages:
        page.setdefault("human_paragraphs", [])
    if old_pages:
        try:
            pages, conflicts = wiki_kernel.preserve_human_pages(pages, old_pages, max_pages)
        except ApplicationError as exc:
            raise APIError(_status_for_kernel(exc.code), str(exc),
                           "invalid_request_error", exc.code) from None
    else:
        conflicts = []
    try:
        local_node = node_identity_plane.local_node_id()
    except Exception:  # noqa: BLE001 -- without identity only foreign rows exist.
        local_node = None
    by_ref = {meta["evidence_id"]: meta for meta in metas}
    for meta in metas:
        if local_node is not None and meta["origin_node_id"] == local_node:
            resolved = await _resolve_local_binding(session, actor, meta)
            meta["resource_id"] = resolved["resource_id"]
            meta["source_version_id"] = resolved["source_version_id"]
            meta["document_id"] = resolved["document_id"]
            meta["source_digest"] = resolved["source_digest"]
            meta["parse_revision"] = resolved["parse_revision"]
    clean_pages, edges = _kernel_revalidate(pages, relations_in, normalized, provider)
    deps = []
    for page in clean_pages:
        refs = {claim["evidence_ids"][n] for section in page["generated_sections"]
                for claim in section["sentences"] for n in range(len(claim["evidence_ids"]))}
        for edge in edges:
            if edge.get("subject_id") == page["page_key"] or edge.get("object_id") == page["page_key"]:
                refs.update(edge.get("evidence_ids") or [])
        for ref in sorted(refs):
            deps.append({**by_ref[ref], "page_key": page["page_key"]})
    if old_pages:
        retained = {p["page_key"] for p in clean_pages if p.get("human_paragraphs")}
        planned = {p["page_key"] for p in pages}
        for dep in old_deps:
            if dep.page_key in retained and dep.page_key not in planned:
                deps.append(wiki_plane.dependency_data(dep))
    provider.update(kind="federated_wiki_generation", federated=True, root_task_id=root_task_id)
    limits = dict((result.get("limits") or {}))
    limits.setdefault("max_pages", max_pages)
    try:
        if wiki_id is None:
            session.add(wiki)
            await session.flush()
        revision = await wiki_plane.append_revision(
            session, actor, wiki, base_id, kind="federated", title=final_title,
            pages=clean_pages, deps=deps, provider=provider, limits=limits,
            conflicts=list(conflicts) + list(result.get("merge_conflicts") or []),
            relations=edges, key=key, request=request, root_task_id=root_task_id)
    except IntegrityError as exc:
        await session.rollback()
        _fail(409, "revision_conflict", f"concurrent federated Wiki write: {type(exc).__name__}")
    try:
        output = await wiki_plane.revision_out(session, actor, wiki, revision.id)
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        _fail(409, "revision_conflict", f"concurrent federated Wiki write: {type(exc).__name__}")
    return output


async def import_bundle_wiki(session, actor, *, wiki_id: str | None, base_revision_id: str | None,
                             title: str, pages: list[dict], deps: list[dict],
                             provider: dict | None = None, limits: dict | None = None,
                             key: str) -> dict:
    """Persist a Bundle-carried Wiki as a real private draft (no auto-publish).

    Bundle import path (BundleCenterClosure calls this; it never forges a
    federated `root_task_id`). Pages are stored verbatim — human paragraphs
    included — with a fresh local dependency manifest pointing at the NEW
    local version/evidence ids the import just created. Kind is
    `bundle_import`; provider records the bundle provenance. Returns the
    existing `revision_out` shape.
    """
    from ddp_corpus.models import Wiki as _Wiki
    if not isinstance(title, str) or not title.strip() or len(title) > 255:
        _fail(400, "wiki_title_invalid", "bundle Wiki needs a title within 1..255 characters")
    if not isinstance(pages, list) or not pages:
        _fail(409, "wiki_generation_invalid", "bundle Wiki holds no pages")
    if not isinstance(deps, list) or not deps:
        _fail(409, "wiki_generation_invalid", "bundle Wiki holds no dependency manifest")
    if not isinstance(key, str) or not key or len(key) > 128:
        _fail(400, "invalid_root_task", "bundle Wiki import needs an idempotency key")
    actor.require(actor.can_upload and actor.principal_id is not None, "编写 Wiki")
    request = {"operation": "bundle_import", "wiki_id": wiki_id,
               "base_revision_id": base_revision_id, "title": title.strip()}
    previous = await wiki_plane.replay(session, actor, key, request)
    if previous:
        return previous
    if wiki_id is None:
        wiki = _Wiki(organization_id=actor.organization_id, owner_id=actor.principal_id,
                     title=title.strip())
        base_id, old_pages = None, []
        session.add(wiki)
        await session.flush()
    else:
        wiki = await wiki_plane.get_wiki(session, actor, wiki_id, write=True)
        if wiki.current_revision_id != base_revision_id:
            _fail(409, "revision_conflict", "Wiki changed; reload before saving")
        base_id = base_revision_id
        _revision, rows, old_deps = await wiki_plane.revision_data(session, base_id)
        if _revision is None or _revision.wiki_id != wiki.id:
            _fail(404, "not_found", "Wiki revision not found")
        await wiki_plane.dependency_state(session, actor, wiki, old_deps)
        old_pages = [{"page_key": row.page_key, "title": row.title,
                      "generated_sections": copy.deepcopy(row.generated_sections),
                      "human_paragraphs": copy.deepcopy(row.human_paragraphs)} for row in rows]
    clean = copy.deepcopy(pages)
    if old_pages:
        try:
            clean, conflicts = wiki_kernel.preserve_human_pages(clean, old_pages, len(clean))
        except ApplicationError as exc:
            raise APIError(_status_for_kernel(exc.code), str(exc),
                           "invalid_request_error", exc.code) from None
    else:
        conflicts = []
    provenance = dict(provider or {})
    provenance.update(kind="bundle_import")
    revision = await wiki_plane.append_revision(
        session, actor, wiki, base_id, kind="bundle_import", title=title.strip(),
        pages=clean, deps=[dict(dep) for dep in deps], provider=provenance,
        limits=dict(limits or {}), conflicts=list(conflicts),
        relations=[], key=key, request=request, root_task_id=None)
    try:
        output = await wiki_plane.revision_out(session, actor, wiki, revision.id)
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        _fail(409, "revision_conflict", f"concurrent Wiki write: {type(exc).__name__}")
    return output
