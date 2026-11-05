# SUBMISSION.md — Submission Checklist and Write-up

Submit on **Wednesday 4 Nov 2026 by 22:00 MYT**. The official deadline is 4 Nov, 9:00 PM ET, which is
**Thursday 5 Nov, 10:00 MYT**. The form's exact fields come from the Official Rules; the list below is our best
expectation and must be checked against them in F0.

## 1. Deadline conversions

| Moment | ET | UTC | MYT (UTC+8) |
| --- | --- | --- | --- |
| Official submission deadline | Wed 4 Nov, 21:00 EST (UTC−5, after US DST ends on 1 Nov) | Thu 5 Nov, 02:00 | **Thu 5 Nov, 10:00** |
| Our internal deadline | Wed 4 Nov, 09:00 | Wed 4 Nov, 14:00 | **Wed 4 Nov, 22:00** |
| Judging | 4–8 Nov | | ≈ 5–9 Nov |
| Closing ceremony and finals | Mon 9 Nov | | ≈ Tue 10 Nov morning |

## 2. What to submit

- [ ] Project name and one-line tagline
- [ ] Track (Indie or Startup) and theme ("Build for Your World" or "Push the Frontier")
- [ ] Description / write-up (draft in §4)
- [ ] Public GitHub repository URL with an MIT license and the English README
- [ ] Demo video URL (YouTube, unlisted), ≤ 3 minutes unless the rules say otherwise
- [ ] Live demo URL (the landing page)
- [ ] How judges can try it (§3)
- [ ] Meta tools used, named precisely: Meta Model API, `muse-spark-1.3`, structured outputs, tool calling, reasoning effort, prompt caching, search grounding; Muse Code (used to build the demo repo)
- [ ] 3–5 screenshots: inline cross-file comment, dashboard lanes, docs patch, eval chart
- [ ] Team member names and roles
- [ ] Confirmation screenshot saved after submitting

## 3. How judges can try it

1. Open the demo repo `winnow-demo-shop` and look at the reviewed PRs (PR1–PR5).
2. Open the live run page for PR1 to see the agent trace and cost.
3. To trigger a fresh review: fork the repo, change any function, and open a PR against `main`. Winnow reviews PRs
   from forks (it only reads them). On the demo repo, `/winnow ask` is enabled for everyone with a cooldown.
4. Or install the App on your own repo from the landing page.

## 4. Write-up draft

> Fill the bracketed values from `eval/report.md`. Keep claims to what the demo and eval show.

**Winnow: an autonomous review-and-docs crew for your repository**

**Inspiration.** On small teams, pull requests wait hours for a reviewer who often lacks context on the rest of the
codebase, and documentation quietly drifts away from the code. We wanted a reviewer that reads the whole repository,
not just the diff, and that keeps the docs honest.

**What it does.** Winnow is a GitHub App. When a pull request opens, a crew of AI agents reviews it and posts inline
comments with one-click fixes, each backed by evidence from the repository. When a public API changes, Winnow also
proposes the matching docstring, README and CHANGELOG updates. A live dashboard shows every agent step, tool call,
token and cent while the review runs.

**How we built it.** A planner splits each PR into review units. A context agent builds a retrieval pack from three
signals: a tree-sitter symbol graph (who calls the changed code), SQLite FTS5 over split identifiers, and local code
embeddings in ChromaDB, fused with Reciprocal Rank Fusion. Four specialists (correctness, security, performance,
maintainability) review in parallel and can search the codebase themselves through tool calls. A verifier checks
every finding against the code and the diff, drops anything without evidence, and a deterministic publisher posts
the survivors. The backend is Python, FastAPI and SQLite on a single Fly.io machine.

**How we use Meta's models.** Every reasoning step runs on Muse Spark 1.3 through the Meta Model API's Responses
endpoint. We set reasoning effort per agent (low for planning, high for security and verification), use structured
outputs so every finding is validated JSON, use parallel tool calling for agentic retrieval, and cache the shared
repository map so repeated calls cost a fraction of the input price. Search grounding checks dependency advisories on
public repos. We also used Muse Code to build the demo repository.

**Results.** On five seeded pull requests (3 repeats each), the full crew found
7.3/10 planted bugs (73% recall; per-case range in `eval/report.md`) with
precision 0.50–1.00 by case, versus 8/10 (80%) for a single-prompt baseline
at $0.01 and ~12 s per PR. The full system's measured edge is qualitative:
it alone cites cross-file evidence (B1: the invoice caller the diff never
touches) and proposes docstring/README/CHANGELOG updates (D1–D3), stays
silent on the clean refactor (PR5 6/6 runs), and filters bogus claims (a
false SSRF was dropped with reasoning). It costs $0.03–0.20 per PR at
35–91 s p50. Multi-unit PRs swing run-to-run (planner coverage varies);
single-unit PRs are stable. Full table and chart: `eval/report.md`,
`eval/report.png`.

**Challenges.** Anchoring comments to exactly the right diff lines; keeping false positives low enough that people
read the comments; and treating pull-request text as untrusted data so that a PR cannot talk the reviewer into
approving it.

**Accomplishments.** Cross-file findings that a diff-only reviewer cannot make; a verifier that measurably improves
precision; documentation that updates itself alongside the code.

**What we learned.** [Fill in honestly after the eval: for example, where full-context beat RAG and where it did not.]

**What's next.** Learning from 👍/👎 feedback per repo, more languages, a sandbox that runs the repo's tests to
validate suggestions, and a hand-off of fixes to Muse Code.

## 5. Final two hours

- [ ] Hosted instance healthy; the demo PRs show completed reviews.
- [ ] README links (video, live URL, App install) all work in a private browser window.
- [ ] Video plays, has sound, is ≤ the limit, and is set to unlisted (not private).
- [ ] Repo is public; no secrets in history (`gitleaks detect`).
- [ ] Tag `v1.0.0` on the submitted commit.
- [ ] Form submitted; confirmation screenshot saved.

## 6. After submitting

- Keep the instance and credits alive until 10 Nov.
- No feature deploys during judging; critical fixes only ([DEPLOYMENT.md §6](DEPLOYMENT.md)).
- Watch Discord and email for judge questions or finalist notices.
