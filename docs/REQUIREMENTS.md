# REQUIREMENTS.md — Winnow Requirements

This document turns the promises in the hackathon application into testable requirements. Each requirement has an
ID, a priority (P0/P1/P2 as in [PLAN.md §6](../PLAN.md)) and acceptance criteria.

## 1. Application promises that must be kept

Application text: *"an autonomous AI workflow assistant powered by Meta's Muse models that helps developers and
small teams automate documentation and code review. It will use a multi-agent architecture and
Retrieval-Augmented Generation (RAG) to understand repository context and suggest real-time improvements."*

| # | Phrase | Chosen interpretation | Requirements |
| --- | --- | --- | --- |
| C1 | autonomous AI workflow assistant | Acts on GitHub events by itself; no manual trigger needed | FR-01, FR-02 |
| C2 | powered by Meta's Muse models | All inference goes through Muse Spark on the Meta Model API | FR-30, NFR-09 |
| C3 | developers and small teams | Installs as a GitHub App in ≤ 5 minutes; configuration is optional | FR-03, NFR-06 |
| C4 | automate documentation | Docstrings, README and CHANGELOG updates proposed when a public API changes | FR-20 – FR-24 |
| C5 | code review | Inline comments with suggestions that can be committed | FR-10 – FR-18 |
| C6 | multi-agent architecture | Planner, 4 specialists, verifier, docs writer, each with its own contract | FR-31, FR-32 |
| C7 | RAG to understand repository context | AST index + FTS + vectors + symbol graph; comments cite other files | FR-04 – FR-08, FR-17 |
| C8 | suggest real-time improvements | Review in about a minute; live trace; CLI before pushing | FR-40 – FR-42, NFR-01 |

Interpretations **not** chosen: "autonomous" does not mean auto-merge or approval; "real-time" does not mean
checking every keystroke inside an IDE.

## 2. Personas

| Persona | Context | Main need |
| --- | --- | --- |
| **Aina, solo developer** | Builds a small SaaS alone; nobody reviews her code | A second pair of eyes that catches bugs before production |
| **Faiz, lead of a 5-person team** | Is the review bottleneck; docs always lag | An automatic first-pass review so he only checks what matters |
| **Mei Ling, open-source maintainer** | PRs from outside contributors who don't know the repo's structure | Comments that explain cross-file impact to contributors |

## 3. User stories

| ID | As | I want | So that |
| --- | --- | --- | --- |
| US-01 | Aina | a review to appear by itself when I open a PR | I don't have to remember to ask |
| US-02 | Faiz | every comment to show its evidence and related files | I can trust it without re-checking |
| US-03 | Faiz | minimal noise | the team doesn't learn to ignore the bot |
| US-04 | Aina | a "Commit suggestion" button for small fixes | I fix things in one click |
| US-05 | Faiz | docstrings and README changes proposed when an API changes | docs stop falling behind |
| US-06 | Mei Ling | to ask about the repo inside the PR thread | I can answer contributors quickly |
| US-07 | Aina | to check my code locally before pushing | I fix issues before the PR opens |
| US-08 | Faiz | to see what each agent did and what it cost | I understand and can control cost |
| US-09 | Mei Ling | the bot to be immune to text planted in a PR | malicious contributors can't manipulate it |
| US-10 | Faiz | to disable specific agents or ignore specific paths | Winnow fits our repo |

## 4. Functional requirements

### 4.1 Installation and indexing

| ID | Requirement | P | Acceptance criteria |
| --- | --- | --- | --- |
| FR-01 | Trigger a review when a PR is `opened`, `synchronize`d, `reopened` or `ready_for_review` | P0 | A new PR on the demo repo gets a review with no manual action |
| FR-02 | Skip draft PRs, PRs by bots, and PRs labelled `winnow:skip` | P0 | A draft PR starts no run; the log states why |
| FR-03 | Index the whole repo when the App is installed | P0 | `installation` event → index completes; status visible on the dashboard |
| FR-04 | AST chunking for Python, TypeScript and JavaScript | P0 | Every function, class and method becomes one chunk with path and line range |
| FR-05 | Line-window fallback chunking for other languages; Markdown split by heading | P0 | A `.go` or `.md` file is indexed without errors |
| FR-06 | Symbol table: definitions and references | P0 | `find_references("apply_discount")` returns every caller |
| FR-07 | Incremental re-index on `push` to the default branch | P1 | Only files whose content hash changed are reprocessed |
| FR-08 | Honour `.gitignore`, `paths.ignore` in `.winnow.yml`, the 200 KB size limit and the secret denylist | P0 | `.env` and `*.pem` never enter the index |

### 4.2 Code review

| ID | Requirement | P | Acceptance criteria |
| --- | --- | --- | --- |
| FR-10 | Inline comments only on RIGHT-side lines that exist in the diff | P0 | Zero 422 errors from GitHub due to invalid lines |
| FR-11 | Every finding has severity, category, title, explanation, evidence and confidence | P0 | Output validates against the `Finding` schema |
| FR-12 | A `suggestion` block when the fix is a local replacement | P0 | Clicking "Commit suggestion" yields code that compiles |
| FR-13 | Summary comment: change walkthrough, findings table, docs patches in a collapsible section | P0 | One summary comment per PR, updated (not duplicated) on later pushes |
| FR-14 | Check run `Winnow review`, `in_progress` → `completed`, conclusion `neutral` | P0 | The check run shows in the PR's Checks tab |
| FR-15 | At most 10 inline comments per run; the rest go in the summary | P0 | Large PRs aren't flooded with comments |
| FR-16 | Don't repeat the same finding on later pushes | P1 | A finding already posted and still valid isn't posted again |
| FR-17 | Cross-file findings: name files the PR didn't touch when relevant | P0 | PR1 produces a comment citing a caller in another file |
| FR-18 | Never approve, request changes or merge | P0 | Reviews are always of type `COMMENT` |

### 4.3 Documentation

| ID | Requirement | P | Acceptance criteria |
| --- | --- | --- | --- |
| FR-20 | Detect public symbols whose signature changed without a docstring update | P0 | PR1 triggers a docstring suggestion |
| FR-21 | Propose the new docstring as an inline `suggestion` block | P0 | The suggestion covers only the docstring lines |
| FR-22 | Find README/docs sections that mention the changed symbol and propose a patch | P0 | The patch passes `git apply --check` at `head_sha` |
| FR-23 | Propose a CHANGELOG entry under `## [Unreleased]` (Keep a Changelog format) | P1 | Entry categorised as Added/Changed/Fixed/Removed |
| FR-24 | `/winnow docs apply` commits the docs patch to the PR branch | P1 | Only for PRs from the same repo; refused for forks |

### 4.4 Interaction

| ID | Requirement | P | Acceptance criteria |
| --- | --- | --- | --- |
| FR-40 | The dashboard streams run events live (SSE) | P0 | Agent lanes change state without a page reload |
| FR-41 | `/winnow review`, `/winnow docs`, `/winnow ask <question>` in PR comments | P1 | Reply in the thread within ≤ 60 s |
| FR-42 | CLI `winnow review <path> --base <ref>` for local checks | P1 | Findings printed in the terminal, coloured by severity |
| FR-43 | 👍/👎 reactions on Winnow comments are recorded | P1 | GitHub sends no webhook for reactions, so they are polled every 5 minutes for open PRs; the `feedback` table updates within 5 minutes |
| FR-44 | `.winnow.yml` controls agents, paths, thresholds and docs targets | P0 | Disabling `security` stops that agent |

### 4.5 AI layer

| ID | Requirement | P | Acceptance criteria |
| --- | --- | --- | --- |
| FR-30 | All LLM calls go through a single `LLMClient` to the Meta Model API | P0 | No other LLM calls in the code (grep check) |
| FR-31 | Each agent has Pydantic input/output and uses structured output | P0 | Invalid output is retried once, then dropped with a log entry |
| FR-32 | Trace every agent step: model, effort, tokens, latency, cost | P0 | The `agent_steps` table is complete for every run |
| FR-33 | Search grounding for dependency CVEs, public repos only | P1 | Automatically disabled for private repos |

## 5. Non-functional requirements

| ID | Category | Requirement | Target |
| --- | --- | --- | --- |
| NFR-01 | Latency | First comment after the PR opens | p50 ≤ 90 s, p95 ≤ 180 s (diff ≤ 500 lines) |
| NFR-02 | Latency | Webhook response | < 1 s (GitHub drops the connection after 10 s) |
| NFR-03 | Cost | Cost per medium PR | ≤ $0.30; configurable daily spend cap |
| NFR-04 | Quality | Precision of posted comments | ≥ 80%; ≤ 1 false positive per PR |
| NFR-05 | Reliability | Idempotent under webhook redelivery | The same delivery never starts two runs |
| NFR-06 | Usability | Time from install to first review | ≤ 5 minutes |
| NFR-07 | Security | See [SECURITY.md](../SECURITY.md) | All P0 controls implemented |
| NFR-08 | Privacy | User code never goes to contributor models; data deleted when the App is uninstalled | Verified by tests |
| NFR-09 | Portability | The LLM provider can be swapped by configuration | `LLMClient` is one interface |
| NFR-10 | Limits | Review size | ≤ 50 files, ≤ 3,000 changed lines; beyond that → partial review + note |
| NFR-11 | Observability | Structured JSON logs carrying `run_id` | Every error can be traced to a run |
| NFR-12 | Availability | During judging, 4–10 Nov | Server up; health check every minute |

## 6. Out of scope

- Auto-merge, approving or blocking PRs.
- GitLab, Bitbucket, Azure DevOps.
- IDE extensions (VS Code, JetBrains).
- Fine-tuning; Muse Spark weights cannot be fine-tuned.
- Billing, paid multi-tenant accounts.
- Executing PR code (except the P2 sandbox, off by default).

## 7. Traceability matrix

| Promise | Requirements | Demo moment ([DEMO.md](DEMO.md)) | Test ([TESTING.md](TESTING.md)) |
| --- | --- | --- | --- |
| C1 | FR-01, FR-02 | 0:35 PR opened, no clicks | `test_webhook_triggers_run` |
| C2 | FR-30 | 2:20 "Built with Muse" panel | `test_llm_client_base_url` |
| C3 | FR-03, NFR-06 | 0:15 App install | Timed demo rehearsal |
| C4 | FR-20 – FR-24 | 1:55 docstring and README patch | `test_docs_patch_applies` |
| C5 | FR-10 – FR-18 | 1:05 inline comment + commit suggestion | `test_line_anchor_valid`, eval |
| C6 | FR-31, FR-32 | 0:50 agent lanes on the dashboard | `test_orchestrator_dag` |
| C7 | FR-04 – FR-08, FR-17 | 1:20 comment citing another file | eval PR1, `test_find_references` |
| C8 | FR-40 – FR-42, NFR-01 | On-screen clock at 0:35–1:05 | latency eval |
