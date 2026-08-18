# DeepDocParse

[中文](README.md) · Apache-2.0 · **Document parsing powered by [MinerU](https://github.com/opendatalab/MinerU)**

A multimodal document-understanding **service layer**. Stateless, GPU-optional, three planes:

| Plane | Endpoint | Engine |
|---|---|---|
| Parsing | `POST /v1/parse` (async task) | MinerU (official `mineru-api` / `mineru-router`, never reimplemented), or the built-in born-digital fallback |
| VQA | `POST /v1/chat/completions` (OpenAI protocol) | DeepSeek-OCR |
| MCP | `ask_document` — one composite tool | Orchestration layer (parse cache → retrieve → bbox crop + VQA) |

Architecture decisions live in [../ARCHITECTURE.md](../ARCHITECTURE.md).

## What this project is trying to be

**A reference implementation of verifiable provenance — not a smaller RAGFlow.**

Every project in this category claims to support grounded citation, and no public
benchmark measures bbox-level citation correctness. So the claim has never been proven
by anyone. This project bets everything on that one thing:

- **Three-part provenance** — every piece of evidence carries a page number, a bbox, and a
  crop rendered from the original file. You can click it and check it.
- **Visual verification** — the crop is fed back to a vision model rather than trusting
  text similarity alone.
- **Degradation must be visible** (an architectural rule): no retrieval hits, embedding
  down, vision model unavailable, crop failed, parse looks wrong — each is labelled on the
  answer. Never a silent fallback. This project has been burned by exactly that before
  (vector retrieval quietly fell back to BM25 and nobody noticed for a long time).

Competing on feature count against RAGFlow / Docling / WeKnora is a losing game, and
winning it would not make anyone choose this project. Hence an equally important
[**explicit non-goals**](#explicit-non-goals) list.

## Quick start (no GPU required)

"You need a GPU or it does not run at all" used to be the single biggest adoption blocker.
It no longer is:

```bash
cp .env.example .env        # set a real SERVICE_TOKEN

# One-time: only vector retrieval needs weights
# (TEI accepts safetensors only, and BAAI ships bge-m3 as .bin)
python scripts/prepare_bge_m3.py

cd docker
docker compose -f compose.cpu.yml --env-file ../.env up -d --build
# gateway:  http://localhost:9000   contract docs: http://localhost:9000/docs
```

> Skipping the ~2GB download: comment out the `embed` service in `compose.cpu.yml` together
> with `embedding_models` in `models.cpu.yaml`. With no embedding section in the registry the
> service falls back to BM25 on its own (a registry-driven switch, no code change).
> **But DeepDocParse-Web's indexing will then fail** — the product layer's QA depends on the
> vector index. Only worth skipping if you use the parsing/MCP planes alone.

This profile uses the **born-digital engine** (`pypdfium2` pulls the text layer and its
coordinates, in-process, no model downloads, no GPU). Provenance is fully intact: page,
bbox and crop.

**Covered**: PDFs with a text layer — papers, reports, contracts.
**Not covered, deliberately**: scanned pages, table structure, formulas. Those need OCR;
enable MinerU instead (there is a commented-out block in both `docker/compose.cpu.yml`
and `models.cpu.yaml`; it is slow on CPU).

**No VQA in this profile.** That is not a missing piece: answers still come back, labelled
`degraded="vision_unavailable"`, so the user can see that visual verification did not
happen. The degradation paths were designed for exactly this.

With a GPU, switch to the full profile (MinerU pipeline + DeepSeek-OCR + TEI):

```bash
docker compose -f compose.dev.yml --env-file ../.env up -d --build
```

## Layout of the repository

```
DeepDocParse/
├── openapi.yaml            # The contract with DeepDocParse-Web (the only coupling surface)
├── models.yaml             # Model registry: adding a model = a container + one line here
├── models.cpu.yaml         # No-GPU registry (born-digital + TEI CPU, no VQA)
├── gateway/                # The only bespoke service: a thin adapter layer
│   └── app/
│       ├── routers/        # parse / chat / embeddings / health
│       ├── services/       # engines (adapter layer) / layout (normalizer) / borndigital
│       │                   # / mineru_client / task_store / chunking
│       └── worker/         # ARQ chain: poll → archive → callback → chunk & index
├── mcp_server/             # FastMCP: ask_document
├── docker/                 # compose.cpu.yml (no GPU) / compose.dev.yml / compose.prod-nvidia.yml
├── docs/                   # layout-format.md (DDP-Layout v1) · CONFIG.md · mineru-api-contract.md
├── scripts/                # check_contract · gen_config_docs · make_fixtures · e2e_mcp
└── tests/                  # Contract tests (must be green before upgrading MinerU)
```

## Configuration

All 12 settings are documented in [docs/CONFIG.md](docs/CONFIG.md), generated from
`gateway/app/config.py` by `scripts/gen_config_docs.py`. CI fails if it goes stale.

## Adding a parse engine

The registry is the only place you touch:

```yaml
parse_engines:
  my-engine:
    endpoint: "http://my-engine:8000"
    runtime: mineru-api         # which adapter speaks to it (see gateway/app/services/engines.py)
    capabilities: [parse]
```

Write a normalizer that emits [DDP-Layout v1](docs/layout-format.md), run
`layout.validate()` to confirm nothing promised is missing, and add the line above.
**No consumer needs to change.** The born-digital engine exists partly as proof: it is the
second engine, and it went in without touching any consumer.

## Explicit non-goals

This says more about what the project is than a feature list does. Each row carries the
condition under which it would be reconsidered — if the condition does not hold, the
answer stays no and the argument is not reopened.

| Not doing | Who has it | What would change our mind |
|---|---|---|
| GraphRAG / RAPTOR | RAGFlow, WeKnora | The eval set shows a significant share of cross-chunk synthesis questions |
| Connectors (drives, wikis, mail) | Onyx, WeKnora | Document sources live outside this system. Upload is enough |
| Multi-channel IM delivery | WeKnora | Never in the main repo (fine as a side project) |
| Workflow orchestration | Dify | Never — different product category |
| Agentic multi-hop retrieval | RAGFlow, WeKnora | Cross-document comparison use cases appear. Single-document QA is inherently single-turn |
| Permissions / RBAC | Onyx, WeKnora | The target shifts to multi-user enterprise deployment |
| ColPali-style visual retrieval | Morphik | The eval set proves a real gap (also: pgvector has no multi-vector MaxSim) |
| **Manual chunk editing** | RAGFlow | Never — with one maintainer and an eval set, fix the chunker, don't have users patch it by hand. Only the read-only boundary overlay stays |
| LoRA training / parametric memory | — | Violates the cross-cutting rule below |
| Reimplementing MinerU | — | Never. Cost of keeping it: one attribution line. Cost of replacing it: training five models |
| Format breadth (audio/video/mail) | Docling | Format breadth ≠ layout understanding. Different problems |

> **Cross-cutting rule: anything that compresses information into weights or a latent space
> conflicts with verifiable provenance.** Such methods may only be used *after* provenance
> has been established (e.g. "read this cropped region and answer"), never inside the
> locating path — once information enters a latent space you can no longer point back to a
> bbox.

## Principles

1. **Never reimplement mineru-api / mineru-router** — official components, configured not rewritten
2. **Never queue twice** — inference queuing belongs to MinerU; ARQ only orchestrates post-processing
3. **Registry-driven** — the gateway imports no model code; it looks up `models.yaml` and forwards
4. **Contract first** — changing `/v1/*` means changing `openapi.yaml` first; CI enforces it
5. **Stateless** — results are cached for 24h; permanent archival belongs to the product layer

## License

[Apache-2.0](LICENSE). Third-party components and attributions: [NOTICE](NOTICE).

**MinerU attribution (required)**: document parsing in this project is powered by
[MinerU](https://github.com/opendatalab/MinerU). MinerU adds terms on top of Apache-2.0;
§2 requires any product providing an **online service** built on MinerU to state clearly
and prominently, in the product UI or public documentation, that it uses MinerU, and §3
terminates the license automatically on violation. This project is exactly such a service,
so the attribution appears at the top of this README, in NOTICE, and in the product UI
footer. Please do not remove it.
