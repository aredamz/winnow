# API.md — Interfaces

Winnow has five interfaces: the GitHub webhook, a small HTTP API with an SSE stream, PR comment commands, the
`.winnow.yml` config file and the CLI. This document also lists every outbound call to GitHub and the shape of
calls to the Meta Model API.

## 1. HTTP endpoints

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| `POST` | `/webhooks/github` | HMAC (`X-Hub-Signature-256`) | Receive GitHub events |
| `GET` | `/healthz` | none | Liveness: DB reachable, queue workers alive |
| `GET` | `/` | none | Landing page with the install button |
| `GET` | `/runs` | allow-listed repos only | Run list |
| `GET` | `/runs/{run_id}` | allow-listed repos only | Live run page |
| `GET` | `/api/runs?repo=&pr=` | allow-listed repos only | Runs as JSON |
| `GET` | `/api/runs/{run_id}` | allow-listed repos only | Run detail: steps, findings, cost |
| `GET` | `/api/runs/{run_id}/events` | allow-listed repos only | Server-Sent Events stream |
| `POST` | `/api/repos/{repo_id}/reindex` | `Authorization: Bearer $WINNOW_ADMIN_TOKEN` | Force a full re-index |

Status codes:

| Code | When |
| --- | --- |
| `202` | Webhook accepted and queued |
| `204` | Webhook event ignored (not handled, draft PR, bot author) |
| `401` | Missing or invalid signature / admin token |
| `404` | Run doesn't exist or its repo isn't allow-listed (same response, by design) |
| `413` | Webhook body over 10 MB |
| `429` | Admin endpoint rate limit |

## 2. Webhook events

| Event | Action | Behaviour |
| --- | --- | --- |
| `ping` | — | `200` |
| `installation` | `created` | Upsert installation and repos; enqueue an `index` job per repo |
| `installation` | `deleted` | Delete all data for the installation ([SECURITY.md §7](../SECURITY.md)) |
| `installation_repositories` | `added` / `removed` | Index the new repos / delete removed repos' data |
| `pull_request` | `opened`, `synchronize`, `reopened`, `ready_for_review` | Enqueue `review` with `dedupe_key = review:<repo>:<pr>:<head_sha>`; cancel the PR's running run |
| `pull_request` | `closed` | Cancel any running run |
| `push` | to the default branch | Enqueue an incremental `index` job |
| `issue_comment` | `created`, on a PR, body starts with `/winnow` | Enqueue a `command` job |
| anything else | — | `204` |

Processing order inside the handler: read the raw body → verify HMAC → check `X-GitHub-Delivery` against
`webhook_deliveries` → parse → filter → insert job → `202`. Nothing slow happens before the response.

## 3. SSE stream

`GET /api/runs/{run_id}/events` sends events defined in [MULTI_AGENT.md §10](MULTI_AGENT.md):

```text
event: agent.finished
id: 42
data: {"run_id": 17, "agent": "security", "findings": 2, "tokens_in": 28111, "tokens_cached": 9120, "tokens_out": 2410, "cost_usd": 0.0466, "latency_ms": 21480}

event: run.finished
id: 57
data: {"run_id": 17, "duration_ms": 71230, "cost_usd": 0.2140}
```

- `id` is the `agent_steps.id` (or a run-level sequence), so a reconnect with `Last-Event-ID` replays missed events
  from the database.
- A heartbeat comment (`: ping`) every 15 s keeps proxies from closing the stream.
- The stream closes after `run.finished` or `run.failed`.

## 4. PR comment commands

| Command | Effect | Priority |
| --- | --- | --- |
| `/winnow review` | Re-run the full review on the current head | P1 |
| `/winnow docs` | Run the docs agent only | P1 |
| `/winnow docs apply` | Commit the latest proposed docs patches to the PR branch | P1 |
| `/winnow ask <question>` | Answer in the thread with `path:line` citations | P1 |
| `/winnow help` | List the commands | P1 |

Rules:

- Accepted only when `comment.author_association` is `OWNER`, `MEMBER` or `COLLABORATOR`; otherwise ignored silently.
  Exception: when the repo sets `commands.allow_public: true` (used only on the public demo repo so judges can try
  it), anyone may use `/winnow ask` and `/winnow help`. `review` and `docs` commands stay restricted.
- `/winnow docs apply` is refused on PRs whose head repo differs from the base repo (forks).
- Cooldown: one command per user per PR every 60 s.
- Acknowledgement: Winnow adds an 👀 reaction to the command comment as soon as the job is queued.

## 5. `.winnow.yml`

Read from the PR's base branch (so a PR can't disable its own review).

```yaml
version: 1
agents:
  correctness: true
  security: true
  performance: true
  maintainability: true
  docs: true
review:
  severity_threshold: medium      # low | medium | high | critical
  max_inline_comments: 10
  skip_drafts: true
  skip_labels: ["winnow:skip"]
paths:
  ignore:
    - "migrations/**"
    - "**/*.generated.ts"
docs:
  targets: [docstrings, readme, changelog]
  changelog_path: CHANGELOG.md
  docstring_style: google         # google | numpy | sphinx | jsdoc
commands:
  allow_public: false             # true only on the public demo repo: /winnow ask for everyone
comment_language: en
```

The file is validated with Pydantic. An invalid file doesn't block the review: defaults are used and the summary
comment shows a warning naming the bad field.

## 6. CLI

| Command | Purpose |
| --- | --- |
| `winnow smoke` | Meta Model API smoke test: text, JSON schema, tool call, cache ([PLAN.md F0](../PLAN.md)) |
| `winnow index <path>` | Index a local repo |
| `winnow ask <path> "<question>"` | Ask a question; answer with citations |
| `winnow review <path> [--base main] [--mode rag\|baseline\|full] [--json]` | Review local changes against a base ref |
| `winnow serve [--port 8000]` | Run the webhook server and dashboard |
| `winnow eval [--cases eval/cases] [--modes baseline,rag] [--repeats 3]` | Run the benchmark ([TESTING.md §5](TESTING.md)) |

`winnow review` exit codes: `0` no findings at or above the threshold, `1` findings, `2` error. This lets it run as
a git `pre-push` hook.

## 7. Outbound GitHub calls

| Call | Used for |
| --- | --- |
| `POST /app/installations/{installation_id}/access_tokens` | Installation token (JWT-signed request) |
| `GET /repos/{owner}/{repo}/pulls/{number}` with `Accept: application/vnd.github.diff` | The PR diff |
| `GET /repos/{owner}/{repo}/pulls/{number}/files` | Changed file list and statuses |
| `GET /repos/{owner}/{repo}/tarball/{ref}` | Snapshot for indexing |
| `GET /repos/{owner}/{repo}/contents/.winnow.yml?ref={base}` | Repo config |
| `POST /repos/{owner}/{repo}/pulls/{number}/reviews` | Review with `event: COMMENT` and inline `comments[]` (`path`, `line`, `side: RIGHT`, optional `start_line`) |
| `POST /repos/{owner}/{repo}/issues/{number}/comments` | Summary comment (first run) and command replies |
| `PATCH /repos/{owner}/{repo}/issues/comments/{comment_id}` | Update the summary comment |
| `POST /repos/{owner}/{repo}/check-runs` / `PATCH .../check-runs/{id}` | `Winnow review` check run |
| `POST /repos/{owner}/{repo}/issues/comments/{comment_id}/reactions` | 👀 acknowledgement |
| `GET /repos/{owner}/{repo}/pulls/comments/{comment_id}/reactions` | Poll 👍/👎 feedback (reactions have no webhook) |
| Git Data API: `POST git/blobs`, `POST git/trees`, `POST git/commits`, `PATCH git/refs/heads/{branch}` | P1 `/winnow docs apply`: one atomic multi-file commit |

All calls go through `github/client.py`, which adds the installation token, retries 5xx and secondary rate limits
with backoff, and logs the request ID.

## 8. Meta Model API call shape

```python
from openai import AsyncOpenAI

client = AsyncOpenAI(base_url="https://api.meta.ai/v1", api_key=settings.model_api_key)

resp = await client.responses.create(
    model="muse-spark-1.3",
    instructions=system_prompt,              # stable → cached prefix
    input=[
        {"role": "user", "content": repo_map_block},   # stable per repo@sha → cached
        {"role": "user", "content": unit_block},       # changes per call
    ],
    reasoning={"effort": "medium"},
    tools=RETRIEVAL_TOOLS,
    parallel_tool_calls=True,
    text={"format": {"type": "json_schema", "name": "findings",
                     "schema": FINDINGS_SCHEMA, "strict": True}},
    max_output_tokens=8000,
    extra_body={"prompt_cache_retention": "24h"},
)
usage = resp.usage   # input_tokens, input_tokens_details.cached_tokens, output_tokens
```

Tool definition (Responses format):

```python
{
    "type": "function",
    "name": "find_references",
    "description": "List every place in the repository that references a symbol.",
    "parameters": {
        "type": "object",
        "properties": {"symbol": {"type": "string"}},
        "required": ["symbol"],
        "additionalProperties": False,
    },
    "strict": True,
}
```

To confirm in the F0 smoke test: `strict` JSON schema support, the exact name and placement of the cache-retention
parameter, and the `cached_tokens` usage field. Adjust this section with what the smoke test shows.
