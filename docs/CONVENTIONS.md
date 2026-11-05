# CONVENTIONS.md — Code, Commit and Prompt Conventions

Rules that keep an 8-day codebase readable on day 8. When a rule gets in the way of shipping a gate, break it
deliberately and leave a `TODO(post-hackathon):` comment.

## 1. Tooling

| Tool | Setting |
| --- | --- |
| Python | 3.12 |
| Packages | uv; `uv.lock` committed; add a dependency only with a reason in the commit message |
| Lint + format | ruff, line length 100, `ruff format` |
| Types | pyright, `basic` for `tests/`, `strict` for `src/winnow/agents`, `github`, `security` |
| Tests | pytest + pytest-asyncio |
| Pre-commit | ruff, ruff-format, gitleaks |

## 2. Code

- **Layout** as in [ARCHITECTURE.md §6](ARCHITECTURE.md). One responsibility per module.
- **Naming:** modules and functions `snake_case`; classes and Pydantic models `PascalCase`; constants `UPPER_SNAKE`.
- **Async** for all I/O (HTTP, LLM, DB via a thread pool where needed). No blocking calls inside `async def`.
- **Types** on every public function. Pydantic models at every boundary: webhook payloads, agent inputs/outputs,
  config.
- **Config** only through `winnow.config.Settings` (pydantic-settings). No `os.environ` anywhere else.
- **Errors:** custom exceptions in `winnow/errors.py`. Never swallow an exception; agent failures are recorded in
  `agent_steps.error` and the run continues where the failure-mode table allows
  ([MULTI_AGENT.md §11](MULTI_AGENT.md)).
- **Logging:** structlog, JSON output; always bind `run_id` (and `agent` inside agents). Never log code, prompts,
  tokens or keys.
- **Global state:** only `Settings`, the event bus and the DB engine.

## 3. LLM usage

- Every LLM call goes through `LLMClient`. A grep test enforces it ([TESTING.md §4](TESTING.md)).
- Prompts live in `src/winnow/llm/prompts/<agent>.md`, starting with a version line such as `# security v2`. No
  prompt strings longer than one line inside Python code.
- Output schemas live in `agents/schemas.py` and nowhere else.
- Untrusted text is always wrapped in `<untrusted source="...">` by one helper, `wrap_untrusted()`.
- Any prompt change after the F6 freeze requires a re-run of the eval and a note in the commit message.

## 4. Tests

- File names `test_<module>.py`; test names `test_<behaviour>` that read as a sentence.
- Every bug fix adds a test that fails before the fix.
- CI runs with `WINNOW_LLM_MODE=replay`; no network in unit or integration tests.
- Fixtures are recorded only from the public demo repo.

## 5. Git

- **Branching:** trunk-based on `main` during the hackathon; short-lived branches are optional. From F4 on, open PRs
  even when solo, so Winnow reviews its own code (dogfooding).
- **Commits:** Conventional Commits — `feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `chore:`, `eval:`, `perf:`.
  Small and frequent; the subject line says what changed and, when not obvious, why.
- **No AI co-author or attribution trailers** in commit messages or PR descriptions.
- **Tags** at gates: `v0.1.0` (G1), `v0.2.0` (G2), `v0.3.0` (G3), `v0.4.0` (G4), `v0.5.0` (G5), `v0.9.0` (freeze),
  `v1.0.0` (submitted commit).
- **Never commit** `.env`, keys, `*.pem`, `data/` or eval outputs containing private code.

## 6. Documentation

- Update the affected doc in the same commit as the behaviour change.
- A decision that changes the architecture gets a new ADR in [DECISIONS.md](DECISIONS.md).
- Update the gate status in [PLAN.md](../PLAN.md) every evening.
- English throughout; plain words, short sentences, tables for comparisons.

## 7. Definition of done for a task

- [ ] Code merged to `main`, ruff and pyright clean.
- [ ] Tests added or updated and passing.
- [ ] Docs updated where behaviour changed.
- [ ] For agent or prompt changes: a short eval run shows no drop in recall or precision.
- [ ] The PLAN checkbox ticked.
