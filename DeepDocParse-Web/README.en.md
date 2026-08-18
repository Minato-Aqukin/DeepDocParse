# DeepDocParse-Web

[中文](README.md) · Apache-2.0 · **Document parsing powered by [MinerU](https://github.com/opendatalab/MinerU)**

The product layer: frontend + backend monorepo. System architecture lives in
[../ARCHITECTURE.md](../ARCHITECTURE.md).

This is where "verifiable provenance" actually reaches a user: every piece of evidence
behind an answer carries a page number, a bbox and a crop of the original region — and
**every degradation is labelled** (no retrieval hits, embedding down, vision model
unavailable, cannot crop, parse looks wrong). Positioning and the explicit non-goals list
live in [../DeepDocParse/README.en.md](../DeepDocParse/README.en.md).

```
DeepDocParse-Web/
├── backend/    # FastAPI: users, API keys, quota, archival, chunk+index, document QA, /v1 and /mcp proxy
├── frontend/   # Vue 3 + TS + Element Plus: library, three-pane workbench, search, usage
├── docker/     # compose.web.yml: PostgreSQL(pgvector) + MinIO + Redis
├── docs/       # DESIGN.md · EVAL.md (citation evaluation) · CONFIG.md
├── eval/       # citations.json — the citation eval dataset
└── scripts/    # e2e_web.py (full-stack) · eval_citations.py · gen_config_docs.py
```

Backend modules: `chunking` · `indexing` · `search` (pgvector / in-memory) · `qa`
(orchestration) · `crops` (provenance cropping) · `archive` · `reconcile` · `gc`.

Calls into DeepDocParse depend **only** on
[../DeepDocParse/openapi.yaml](../DeepDocParse/openapi.yaml).

## Three kinds of caller, three kinds of credential

| Caller | Credential | Entry point |
|---|---|---|
| Web user | JWT (obtained by logging in) | `/api/*` |
| Third-party developer | API key `sk-…` (hashed at rest, shown once) | `/v1/*`, `/mcp` |
| DeepDocParse (service) | Internal `SERVICE_TOKEN` | `/internal/parse-callback` |
| Anyone holding the token | One-off random token | `/files/{token}` original download |

The service layer never sees user credentials: this layer validates the key/JWT and
forwards with `SERVICE_TOKEN`.

## Quick start (no GPU required)

```bash
cp .env.example .env          # SERVICE_TOKEN must match DeepDocParse/.env

# 1. Stateful components (PostgreSQL 15432 / MinIO 19000, console 19001)
cd docker && docker compose -f compose.web.yml --env-file ../.env up -d

# 2. backend (8080)
cd backend && ../.venv/bin/alembic upgrade head
../.venv/bin/python -m uvicorn app.main:app --port 8080 --reload

# 3. frontend (5173, already proxied to 8080)
cd frontend && npm install && npm run dev
```

For the service side use the **CPU quick start** in
[../DeepDocParse/README.en.md](../DeepDocParse/README.en.md) — no GPU needed. Answers come
back labelled `degraded="vision_unavailable"`; that is a designed path, not a failure.

**The one thing you must supply yourself is an OpenAI-compatible chat endpoint.** This
layer only requires protocol compatibility and is not tied to how DeepDocParse is deployed
(ADR #17). Local llama.cpp, Ollama, or any hosted API works:

```bash
# .env
CHAT_URL=http://127.0.0.1:11434/v1/chat/completions
CHAT_MODEL=qwen3:8b
```

> On Windows replace `.venv/bin/` with `.venv/Scripts/`.

## Key flows

**Upload → archive**: compute the file's content sha256 as `doc_id` → store in MinIO →
mint a stable file URL → `POST service /v1/parse{file_url, doc_id, callback_url}` →
callback or reconciliation triggers archival → data-URI images are decoded to objects and
markdown references rewritten → metered per page.

**Why reconciliation exists**: the gateway's completion callback is best-effort, and a
backend restart at the wrong moment would lose the result permanently (the service only
caches for 24h). `app/reconcile.py` sweeps unfinished tasks every 60s.

**Why a stable file URL instead of presigned**: presigned URLs expire and their signature
is bound to the host; worse, the MCP `ask_document` plane only receives a bare URL, so a
changing URL means the vector index never hits. See ADR #12 (and ADR #11 for document
identity).

**Document QA**: this layer chunks the archived `layout.json` itself → embeds → stores in
Postgres+pgvector. On a question: hybrid retrieval (vector + keyword, fused with RRF) →
crop the region by bbox → multimodal answer → SSE stream, with page/bbox/crop provenance.

Provenance carries a **stable locator** `(parse_job_id, seq)` alongside the chunk id:
chunk ids are re-minted on every reindex, so storing only the id would mean losing "which
passage was this answer based on" after a single rebuild. Answers also record a
`model_meta` fingerprint (chat model, embedding model, retrieval parameters) — without it,
historical answers cannot be grouped and compared after a model change.

## Citation evaluation

Verifiable provenance has to be **measured**, not merely claimed:

```bash
python scripts/eval_citations.py --mode offline   # no models or services needed
python scripts/eval_citations.py --mode live      # full stack, all four metrics
```

Four metrics (citation page hit rate, bbox containment, refusal correctness, degradation
label accuracy), **reported per attribute slice, never as one aggregate score** — an
aggregate tells you "3% better", a slice tells you "two-column pages hit only 40%".
Definitions, dataset format and current findings: [docs/EVAL.md](docs/EVAL.md).

## Configuration

All 45 settings: [docs/CONFIG.md](docs/CONFIG.md), generated from `backend/app/config.py`
by `scripts/gen_config_docs.py`. CI fails if it goes stale.

## Verification

```bash
cd backend && ../.venv/bin/python -m pytest    # unit tests: SQLite in-memory + respx, no PG/MinIO
cd frontend && npm run build                    # includes vue-tsc type checking
.venv/bin/python scripts/e2e_web.py             # full stack against real PG/MinIO/service
```

## License

[Apache-2.0](LICENSE). Third-party components and attributions: [NOTICE](NOTICE).

**MinerU attribution (required)**: document parsing is powered by
[MinerU](https://github.com/opendatalab/MinerU). Its additional terms (§2) require any
online service built on MinerU to state that clearly and prominently in the product UI or
public documentation; §3 terminates the license automatically on violation. This layer is
the user-facing end, so the attribution appears at the top of this README and in a footer
on **every page** of the product (`frontend/src/layouts/AppShell.vue`). Please do not
remove it.
