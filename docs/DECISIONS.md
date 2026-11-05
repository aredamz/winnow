# DECISIONS.md — Architecture Decision Records

Short records of decisions that shape the code. Add a new ADR instead of editing an accepted one; mark the old one
"Superseded by ADR-NNN".

| ADR | Decision | Status |
| --- | --- | --- |
| 001 | Python + FastAPI in a single process | Accepted |
| 002 | Deliver as a GitHub App, not a GitHub Action | Accepted |
| 003 | Own asyncio orchestrator instead of LangGraph | Accepted |
| 004 | Local embeddings, because the Meta Model API has no embeddings endpoint | Accepted, verify in F0/F1 |
| 005 | Hybrid retrieval: symbol graph + FTS5 + vectors, fused with RRF | Accepted |
| 006 | Responses API with structured outputs | Accepted, verify in F0 |
| 007 | Suggestions only: `COMMENT` reviews, no approval or merge | Accepted |
| 008 | SQLite + ChromaDB on one volume | Accepted |
| 009 | Host on Fly.io | Proposed (PLAN Q8) |
| 010 | Deterministic verifier gate and publisher | Accepted |
| 011 | Contributor models only for public-repo evals | Accepted |
| 012 | Server-rendered dashboard: Jinja + HTMX + SSE | Accepted |
| 013 | One model for every agent; vary reasoning effort instead | Accepted |

---

## ADR-001 — Python + FastAPI in a single process

- **Context:** 8 days, solo; existing Python RAG experience; the demo needs stability more than scale.
- **Decision:** one Python 3.12 process running FastAPI, the job queue and the workers.
- **Alternatives:** TypeScript/Node (fewer existing skills); separate web and worker services (more moving parts).
- **Consequences:** simple deploy and debugging; scaling requires the split described in
  [ARCHITECTURE.md §8](ARCHITECTURE.md).

## ADR-002 — GitHub App, not GitHub Action

- **Context:** reviews must start on their own, quickly, with a shared repository index.
- **Decision:** a GitHub App receiving webhooks.
- **Alternatives:** a GitHub Action running in the user's CI. It would rebuild the index on every run, need the
  user to store a Meta API key in their secrets, and start only after CI queues.
- **Consequences:** we host a server and manage App credentials; installation is one click; the index persists.
  An Action wrapper around the CLI remains possible later.

## ADR-003 — Own asyncio orchestrator instead of LangGraph

- **Context:** the flow is a fixed fan-out/fan-in graph; the provider (Meta Model API) is new and framework support
  for it is unproven; tracing must feed our own dashboard.
- **Decision:** about 300 lines of asyncio code with typed Pydantic contracts between agents.
- **Alternatives:** LangGraph (familiar from LangChain, but an extra abstraction over a new provider); CrewAI.
- **Consequences:** full control over retries, budgets, cancellation and events; the explicit design is itself
  "technical depth" material; no framework features such as checkpointing for free.

## ADR-004 — Local embeddings

- **Context:** the Meta Model API docs list no embeddings endpoint. RAG still benefits from a semantic signal.
- **Decision:** `jinaai/jina-embeddings-v2-base-code` via fastembed (ONNX, CPU), with `all-MiniLM-L6-v2` as
  fallback; final choice from the F1 spike.
- **Alternatives:** a third-party embeddings API (another vendor receiving user code); no vectors at all (graph + FTS
  only).
- **Consequences:** no code leaves the server for embedding; indexing uses local CPU; image size grows by the model.
- **Verify:** F0 — call `/v1/embeddings` and record the response here; F1 — model availability and speed.
- **Verified 27 Oct (smoke_muse.py [6]):** `POST https://api.meta.ai/v1/v1/embeddings` → HTTP 404.
  No embeddings endpoint. Local-embeddings decision stands; F1 spike picks the model.
- **Spike 28 Oct (`scripts/embedding_spike.py`, 10 queries over demo repo):**
  `jina-embeddings-v2-base-code` hit@5 = 1.0 (index 16 s, 768 dims);
  `all-MiniLM-L6-v2` hit@5 = 1.0 (index 2 s, 384 dims). Tie on this small
  corpus — kept PRIMARY = jina (code-specialised, richer vectors for larger
  repos), MiniLM stays the fallback (8× faster, smaller image; use it if Fly
  memory bites, risk R9). Revisit on a bigger corpus if eval demands it.

## ADR-005 — Hybrid retrieval with RRF

- **Context:** code review depends on structural relationships (callers, tests) as much as on similar text.
- **Decision:** three retrievers (symbol graph, FTS5 on split identifiers, vectors) fused with Reciprocal Rank Fusion
  (k = 60), then budget-packed by priority ([RAG.md §6](RAG.md)).
- **Alternatives:** vectors only (misses callers); a learned re-ranker (time and cost).
- **Consequences:** better cross-file recall; three indexes to keep consistent during incremental indexing.

## ADR-006 — Responses API with structured outputs

- **Context:** findings must be machine-checkable; agents need tool loops and reasoning carried across turns.
- **Decision:** use the Responses API via the OpenAI Python SDK pointed at `https://api.meta.ai/v1`, with JSON-schema
  structured outputs generated from Pydantic models.
- **Alternatives:** Chat Completions (supported, but Meta's quickstart uses Responses); the Anthropic-compatible
  Messages API.
- **Consequences:** reliable parsing; one repair retry on invalid output.
- **Verify:** F0 smoke test — strict mode, the cache-retention parameter and the `cached_tokens` usage field.
- **Verified 27 Oct (smoke_muse.py):** strict JSON schema accepted (`strict: true`,
  `additionalProperties: false`); parallel tool call round-trip works; `reasoning.effort`
  accepted; usage reports `input_tokens` / `output_tokens`.
  **Open:** two identical consecutive calls returned `cached_tokens: 0` both times, so the
  24 h prefix cache is not yet proven — `extra_body={"prompt_cache_retention": "24h"}` was
  accepted without error, but no cache hit observed. Re-probe in F3 with a realistic-size
  repo-map prefix before claiming cache savings in the budget.
- **Updated 28 Oct (CLI ask failures):** strict mode is mandatory-strict: the API
  rejects with 400 unless EVERY object (including nested `$defs`) sets
  `additionalProperties: false` AND lists every property key in `required`.
  Pydantic omits both for default-valued fields, so `LLMClient.strict_schema()`
  patches generated schemas. All agents must use it (MULTI_AGENT.md §3).

## ADR-007 — Suggestions only

- **Context:** autonomy raises safety and trust concerns; prompt injection is a real threat.
- **Decision:** reviews are always `COMMENT`; changes land only through a human click or command.
- **Alternatives:** auto-fix commits; request-changes reviews that block merges.
- **Consequences:** lower risk and easier adoption; "autonomous" means acting on events, not acting on code.

## ADR-008 — SQLite + ChromaDB on one volume

- **Context:** one machine; a handful of repos during the hackathon.
- **Decision:** SQLite in WAL mode (with FTS5) and a persistent ChromaDB directory under `WINNOW_DATA_DIR`.
- **Alternatives:** Postgres + pgvector (another service to run and pay for).
- **Consequences:** zero-ops; one node only; migration path documented.

## ADR-009 — Host on Fly.io (proposed)

- **Context:** needs an always-on HTTPS endpoint for webhooks and a persistent volume.
- **Decision (proposed):** one Fly.io machine, `auto_stop_machines = "off"`, 3 GB volume.
- **Alternatives:** Modal (partner credit available; serverless model fits webhooks less naturally with a local
  SQLite file); Railway; Render.
- **Consequences:** a small running cost; decide by F4 (PLAN Q8).

## ADR-010 — Deterministic verifier gate and publisher

- **Context:** LLMs can hallucinate line numbers and can be manipulated into unwanted actions.
- **Decision:** deterministic checks run before and after the LLM verifier, and only plain code talks to GitHub.
- **Alternatives:** letting an agent call the GitHub API as a tool.
- **Consequences:** no 422 errors from bad anchors reach users; no LLM can approve, merge or post outside the diff.

## ADR-011 — Contributor models only for public-repo evals

- **Context:** contributor pricing is far cheaper, but it permits Meta to use prompts and completions for training.
- **Decision:** never for user code; allowed only for eval runs on the public demo repo behind
  `WINNOW_ALLOW_CONTRIBUTOR=1`, refused in code for private repos.
- **Consequences:** eval budget goes further; final reported numbers use the standard model.

## ADR-012 — Server-rendered dashboard

- **Context:** the dashboard is one live page plus a list; no frontend build step wanted.
- **Decision:** Jinja2 templates, HTMX with its SSE extension, plain CSS.
- **Alternatives:** React + Vite (build pipeline, more code).
- **Consequences:** fast to build; limited client-side interactivity, which the demo doesn't need.

## ADR-013 — One model, varied effort

- **Context:** `muse-spark-1.3` is Meta's latest model for coding and agentic work; mixing models complicates
  caching and evaluation.
- **Decision:** every agent uses `muse-spark-1.3`; reasoning effort differs per agent (low → high).
- **Alternatives:** an older or cheaper model for the planner.
- **Consequences:** one cache namespace for the shared prefix; simpler eval; cost controlled via effort and budgets.
