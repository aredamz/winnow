# DEMO.md — Demo Repository, Seeded PRs and Video Script

The demo tells one story in under three minutes: **a small team pushes a risky change, and within about a minute
Winnow catches a bug in a file the PR never touched, proposes the fix and the docs update, and refuses to be
talked out of it.** Every promise in the application appears on screen at least once
([REQUIREMENTS.md §7](REQUIREMENTS.md)).

## 1. Demo repository: `winnow-demo-shop`

A small, realistic FastAPI shop, about 1,500 lines of Python, public on GitHub. Built during F1 with help from
Muse Code, which is itself a use of Meta tooling worth mentioning in the write-up.

| Path | Contents |
| --- | --- |
| `shop/pricing.py` | `apply_discount`, `Cart`, coupon rules |
| `shop/money.py` | `Decimal` helpers used everywhere for currency |
| `shop/orders.py` | Create, list, cancel orders |
| `shop/inventory.py` | Async stock reservation |
| `shop/payments.py` | Payment stub |
| `shop/shipping.py` | Shipping fee and free-shipping threshold |
| `billing/invoice.py` | `Invoice.total()`, calls `apply_discount` |
| `api/routes.py` | FastAPI routes; every route uses the `require_user` dependency |
| `db.py` | SQLite access with parameterised queries |
| `tests/` | pytest suite, green on `main` |
| `README.md` | Setup + an "API" section documenting `apply_discount` |
| `CHANGELOG.md` | Keep a Changelog format with an `## [Unreleased]` section |
| `.winnow.yml` | Default config, `commands.allow_public: true` (see [API.md §4](API.md)) |

## 2. Seeded PRs

Each PR lives on its own branch so it can be re-opened fresh for every rehearsal (`scripts/reset_demo.sh` closes the
old PR and opens a new one from the same branch).

| PR | Title | Seeded issues | What it proves |
| --- | --- | --- | --- |
| PR1 | "Use rates instead of percentages for discounts" | **B1** `apply_discount` now returns a fraction; `billing/invoice.py` still multiplies as a percentage (cross-file). **B4** new code uses `float` for money although `shop/money.py` uses `Decimal`. **D1** docstring still documents `percent`. **D2** README "API" section still shows the old signature (cross-file). **D3** no CHANGELOG entry | RAG across files, docs agent, suggestions |
| PR2 | "Add order search endpoint" | **S1** SQL built with an f-string from a query parameter. **S2** hard-coded fake AWS key for "receipt uploads". **S3** the new route lacks `require_user`, unlike every other route (cross-file) | Security specialist, secret redaction |
| PR3 | "Speed up order listing" | **P1** N+1 query loading items per order. **P2** blocking `requests.get` without a timeout inside `async def`. **B2** missing `await` on `reserve_stock` | Performance specialist, async bugs |
| PR4 | "Adjust free-shipping threshold" | **B3** `>` instead of `>=` at the threshold. PR body and a code comment contain prompt-injection text telling AI reviewers to approve | Injection resistance |
| PR5 | "Rename helpers for clarity" | None: a pure rename with every caller updated, tests green | Precision: Winnow should stay nearly silent |

Labels for these live in `eval/cases/` ([TESTING.md §5](TESTING.md)). Cross-file items: B1, D2, S3.

## 3. Video script (target 2:50, hard limit 3:00)

Recorded in segments, then edited. Speed-ups are shown honestly with an on-screen clock, never hidden.

| Time | Screen | Voiceover (English) | Promise |
| --- | --- | --- | --- |
| 0:00–0:15 | Title card with the tagline, then a PR list with "waiting for review" badges | "Small teams ship fast, but their pull requests wait hours for review, and the docs fall behind the code. Meet Winnow: an autonomous review-and-docs crew powered by Meta's Muse Spark." | — |
| 0:15–0:35 | Install the GitHub App on `winnow-demo-shop`; dashboard shows indexing progress | "Install it on a repo. Winnow reads the whole codebase: every function, every caller, every doc." | C3 |
| 0:35–0:50 | Open PR1 in GitHub; the "Winnow review" check turns yellow; a clock appears top-right | "Now a teammate changes how discounts work and opens a PR. Nobody asks for a review." | C1, C8 |
| 0:50–1:05 | Dashboard: planner, then four specialist lanes running in parallel, tool calls flashing, cost ticking | "A planner splits the work. Four specialists, for correctness, security, performance and maintainability, search the repo on their own." | C6 |
| 1:05–1:20 | GitHub: the first inline comment arrives; hover the "Commit suggestion" button | "About a minute later, the first comment lands, with a fix you can commit in one click." | C5 |
| 1:20–1:40 | Zoom into the evidence: `billing/invoice.py:42`, a file not in the PR | "Look at the evidence. The bug isn't in this diff at all. It's in the invoice code that calls this function. That's retrieval over the whole repository, not just the diff." | C7 |
| 1:40–1:55 | PR2 montage: SQL-injection comment; the redacted-secret finding | "On another PR it flags a SQL injection, and a hard-coded key that it redacted before anything left our server." | C5, security |
| 1:55–2:10 | Summary comment: docstring suggestion, README patch, CHANGELOG entry | "Docs too: the docstring, the README section and the changelog are updated to match the new API." | C4 |
| 2:10–2:20 | PR4 body with the injection text; Winnow's comment still reports the threshold bug | "And when a PR tries to talk the reviewer into approving it, Winnow treats that text as data and reports the bug anyway." | security |
| 2:20–2:40 | "Built with Muse" panel (effort per agent, structured outputs, parallel tools, prompt caching, 1M context), then the eval chart: baseline vs Winnow | "Under the hood: Muse Spark 1.3 with per-agent reasoning effort, structured outputs, parallel tool calls and prompt caching. On our seeded benchmark of five pull requests, only the full crew cites cross-file evidence and updates the docs — 7 of 10 planted bugs found, at about 3 to 20 cents a PR. A single prompt is cheaper and matches in-diff bugs, but never leaves the diff." | C2, depth |
| 2:40–2:50 | Landing page with the install button and repo link | "Winnow: every PR reviewed, every doc current, in about a minute. Install it from the link below." | — |

Numbers above are final (eval 28 Oct, frozen prompts, full table with min-max ranges in `eval/report.md`). Never round numbers in our favour.

## 4. Recording setup

- OBS, 1920×1080, 30 fps; browser zoom 125%; GitHub light theme; a clean browser profile with no bookmarks bar.
- Notifications off (macOS Focus mode); close Slack, mail and messaging apps.
- External mic; record the voiceover separately from the screen.
- No "made with" or tool-credit cards in the video.
- Record the golden run on F7 and keep it as the backup if a live take fails.

## 5. Rehearsal checklist

- [ ] `scripts/reset_demo.sh` re-opens PR1–PR5 cleanly.
- [ ] Manual QA list in [TESTING.md §8](TESTING.md) passes.
- [ ] Each segment rehearsed once with a stopwatch; total ≤ 2:50.
- [ ] Rehearsal 3 recorded end to end as a fallback.

## 6. Judge Q&A preparation

| Likely question | Honest answer |
| --- | --- |
| How is this different from existing AI review bots? | Every comment must carry evidence the verifier checked; the trace and cost are fully visible; it updates docs, not just code; and the benchmark shows the gain over a single prompt |
| Why multi-agent instead of one big prompt? | Specialists with narrow focus and their own effort level, plus an independent verifier, measured against a single-prompt baseline in the eval |
| What about false positives? | Evidence-or-silence rule, verifier with a confidence threshold, 10-comment cap, PR5 as a precision test, 👍/👎 feedback |
| Is my private code safe? | Redaction before the model, standard (non-training) tier only, per-repo isolation, deletion on uninstall ([SECURITY.md](../SECURITY.md)) |
| Does it scale to big repos? | Indexing is incremental; RAG keeps context bounded; the scaling path is in [ARCHITECTURE.md §8](ARCHITECTURE.md) |
| Why not Meta embeddings? | The Model API has no embeddings endpoint, so a local code-embedding model complements Muse Spark ([ADR-004](DECISIONS.md)) |
| What did Muse Spark specifically enable? | Parallel tool calls for agentic retrieval, structured outputs for reliable findings, per-agent effort, cheap cached prefixes, and a 1M context for the full-context comparison |
