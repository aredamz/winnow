# DEPLOYMENT.md — Environments, Setup and Runbook

Winnow runs in two places: **locally** with a webhook tunnel during development, and on **one Fly.io machine with a
persistent volume** for the demo and judging. Two separate GitHub Apps keep dev experiments away from the judged
instance.

## 1. Environments

| Environment | Where | GitHub App | Model | Purpose |
| --- | --- | --- | --- | --- |
| Local dev | Laptop + cloudflared or smee tunnel | `winnow-dev` | `muse-spark-1.3` | Building and debugging |
| Eval | Laptop, CLI | none (CLI against local clone) | Standard; contributor allowed for the public demo repo | Benchmark runs |
| Production | Fly.io, 1 machine, US region | `winnow` | `muse-spark-1.3` | Demo, judging, dogfooding |

## 2. Environment variables

| Variable | Required | Example | Notes |
| --- | --- | --- | --- |
| `MODEL_API_KEY` | yes | — | Meta Model API key; separate keys for dev and prod |
| `WINNOW_MODEL` | no | `muse-spark-1.3` | Default model for all agents |
| `WINNOW_LLM_MODE` | no | `live` | `live`, `record`, `replay` ([TESTING.md §2](TESTING.md)) |
| `WINNOW_ALLOW_CONTRIBUTOR` | no | `0` | `1` only on the eval machine ([SECURITY.md §3](../SECURITY.md)) |
| `GITHUB_APP_ID` | yes | `123456` | |
| `GITHUB_APP_SLUG` | yes | `winnow` | Builds the install link on the landing page |
| `GITHUB_APP_PRIVATE_KEY` | prod | PEM contents | Set with `fly secrets` |
| `GITHUB_APP_PRIVATE_KEY_PATH` | local | `~/.config/winnow/dev.pem` | Mode `600`, outside the repo |
| `GITHUB_WEBHOOK_SECRET` | yes | — | `openssl rand -hex 32` |
| `WINNOW_DATA_DIR` | no | `/data` | SQLite + Chroma location |
| `WINNOW_PUBLIC_BASE_URL` | yes | `https://winnow-xyz.fly.dev` | Used in links from GitHub comments to the dashboard |
| `WINNOW_PUBLIC_REPOS` | no | `owner/winnow-demo-shop` | Dashboard allow-list |
| `WINNOW_DAILY_SPEND_USD` | no | `20` | Daily spend cap |
| `WINNOW_ADMIN_TOKEN` | yes | — | Admin endpoints |
| `WINNOW_LOG_PROMPTS` | no | `0` | Must be `0` in production |
| `WINNOW_LOG_LEVEL` | no | `INFO` | |
| `WINNOW_WORKERS` | no | `2` | Concurrent jobs |

## 3. Registering the GitHub Apps

Do this twice: `winnow-dev` (webhook → tunnel URL) and `winnow` (webhook → Fly URL).

1. GitHub → Settings → Developer settings → GitHub Apps → **New GitHub App**.
2. Name, homepage URL (`WINNOW_PUBLIC_BASE_URL`), webhook URL `<base>/webhooks/github`, webhook secret.
   Note (verified 28 Oct): the current GitHub App UI has no event-subscription
   section, and the installation reports `events: []`, so the App webhook
   alone delivers nothing. Fallback used for the demo: a repository webhook on
   the demo repo (same URL, same secret, events pull_request/issue_comment/push;
   id 704076707). The server accepts both shapes; repo-webhook payloads omit
   `installation`, which the runner resolves via the App JWT.
3. Permissions exactly as in [SECURITY.md §5](../SECURITY.md). Nothing else.
4. Subscribe to events: Pull request, Issue comment, Push, Installation, Installation repositories.
5. Where can it be installed: "Only on this account" for `winnow-dev`; "Any account" for `winnow` so judges can try it.
6. Generate a private key; store it as in [SECURITY.md §6](../SECURITY.md); delete the downloaded copy from
   `~/Downloads`.
7. Install the App on `winnow-demo-shop`.

## 4. Local development

```bash
uv sync
cp .env.example .env                     # fill in values; .env is git-ignored
uv run winnow smoke                      # G0: API reachable, schema + tools + cache work

# terminal 1: public tunnel → note the https URL it prints
cloudflared tunnel --url http://localhost:8000
#   or: npx smee-client --url https://smee.io/<channel> --target http://localhost:8000/webhooks/github

# terminal 2: server with reload
uv run winnow serve --reload
```

Set the `winnow-dev` App's webhook URL to the tunnel URL + `/webhooks/github`. A quick tunnel URL changes on every
restart; smee keeps a stable URL.

## 5. Production on Fly.io

### 5.1 First deploy

```bash
fly launch --no-deploy --name winnow-<suffix> --region iad
fly volumes create winnow_data --size 3 --region iad
fly secrets set \
  MODEL_API_KEY=... \
  GITHUB_APP_ID=... GITHUB_APP_SLUG=winnow \
  GITHUB_WEBHOOK_SECRET=... \
  GITHUB_APP_PRIVATE_KEY="$(cat ~/.config/winnow/prod.pem)" \
  WINNOW_ADMIN_TOKEN=... \
  WINNOW_PUBLIC_BASE_URL=https://winnow-<suffix>.fly.dev \
  WINNOW_PUBLIC_REPOS=<owner>/winnow-demo-shop
fly deploy
```

Region: a US East region, because GitHub webhooks originate in the US and most judges are likely there. Measure
round-trip time to `api.meta.ai` from the machine during F5 and move regions if another is clearly faster.

### 5.2 `fly.toml` essentials

```toml
[mounts]
  source = "winnow_data"
  destination = "/data"

[http_service]
  internal_port = 8000
  force_https = true
  auto_stop_machines = "off"     # webhooks must never hit a stopped machine
  min_machines_running = 1

  [[http_service.checks]]
    path = "/healthz"
    interval = "30s"
    timeout = "5s"

[[vm]]
  size = "shared-cpu-2x"
  memory = "2gb"                 # embedding model + Chroma
```

### 5.3 Dockerfile outline

```dockerfile
FROM python:3.12-slim@sha256:<pinned-digest>
ENV PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*            # git is needed for `git apply --check`
COPY --from=ghcr.io/astral-sh/uv:<pinned-version> /uv /bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev
RUN uv run python -m winnow.indexing.embed --warm   # bake the embedding model into the image
RUN useradd --uid 1000 winnow
USER winnow
CMD ["uv", "run", "winnow", "serve", "--host", "0.0.0.0", "--port", "8000"]
```

The volume at `/data` is root-owned on first mount; the entrypoint must `chown` it once (or run a one-off
`fly ssh console -C "chown -R 1000:1000 /data"`).

## 6. Release procedure

1. `pytest` green locally; `ruff` and `pyright` clean.
2. `git tag vX.Y.Z && git push --tags`.
3. `fly deploy`.
4. `curl -fsS $WINNOW_PUBLIC_BASE_URL/healthz`.
5. GitHub App → Advanced → Recent Deliveries → redeliver the last `pull_request` event → a run appears on the
   dashboard.

During judging (4–10 Nov) deploy only critical fixes, and only after the manual QA list in
[TESTING.md §8](TESTING.md).

## 7. Runbook

| Symptom | Check | Fix |
| --- | --- | --- |
| A PR got no review | App → Advanced → Recent Deliveries: status code; `fly logs` | `401` → webhook secret mismatch; `5xx` → read the trace; then redeliver |
| `401 invalid_api_key` from Meta | Key in `fly secrets list` | Reset the secret; redeploy |
| `404 model_not_found` | `WINNOW_MODEL` | Use an ID listed in the Model API docs |
| Many `429`s | `agent_steps.error`; team rate limits | Lower `WINNOW_WORKERS`; the orchestrator falls back to 2 specialists |
| Credits running out | Model API dashboard; `spend_daily` | Reduce specialists; cache more; ask organisers on Discord |
| `422` on review POST | Payload in logs (no code) | Fallback already posts findings in the summary; add a diff fixture and fix line mapping |
| Index stuck at `indexing` | `jobs` table | `POST /api/repos/{id}/reindex` with the admin token |
| Disk usage high | `fly ssh console` → `du -sh /data/*` | Run cleanup; grow the volume |
| Machine restarted mid-run | `jobs` with `running` status | On boot, `running` jobs are re-queued automatically |

## 8. Monitoring

- Fly health check on `/healthz` every 30 s.
- External uptime check against `/healthz` every minute during judging (any free uptime service).
- `spend_daily` reviewed every evening; warning log at 80% of the cap.
- Log lines at `ERROR` level reviewed once a day during judging.

## 9. After the hackathon

- Rotate the App private keys, webhook secrets and `MODEL_API_KEY` (not before 10 Nov).
- Decide: keep the instance running, or `fly scale count 0` and delete the volume.
- Remove `winnow-dev` if it is no longer used.
