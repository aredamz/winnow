"""Deterministic publisher: findings → GitHub review + summary + check run.

Only plain code talks to GitHub (ADR-010): the model never sees API shapes.
Every body passes through the output sanitiser (T3); every anchor was checked
by the verifier, and re-checked here before sending.
"""

from __future__ import annotations

from winnow.agents.schemas import VerifiedFinding
from winnow.github.diff import ParsedDiff
from winnow.security.sanitize import SUMMARY_MARKER, sanitize_markdown


def build_inline_comment(finding: VerifiedFinding) -> dict:
    """One GitHub review comment dict (path, line, side RIGHT, suggestion)."""
    body = (
        f"**[{finding.severity.value.capitalize()}] {finding.category.value}:** {finding.title}\n\n"
        f"{finding.explanation}\n\n<details><summary>Evidence</summary>\n\n"
        + "\n".join(f"- `{e.path}:{e.start_line}-{e.end_line}` ({e.why})" for e in finding.evidence)
        + "\n\n</details>\n"
    )
    if finding.suggestion and finding.suggestion.strip():
        body += f"\n```suggestion\n{finding.suggestion.strip()}\n```\n"
    body += (
        f"\n<sub>Winnow · {finding.agent} agent · "
        f"confidence {finding.confidence:.2f} · react 👍 or 👎 to tune future reviews</sub>"
    )
    comment: dict = {
        "path": finding.path,
        "line": finding.end_line,
        "side": "RIGHT",
        "body": sanitize_markdown(body),
    }
    if finding.start_line != finding.end_line:
        comment["start_line"] = finding.start_line
    return comment


def split_inline(
    kept: list[VerifiedFinding], diff: ParsedDiff, max_inline: int = 10
) -> tuple[list[dict], list[VerifiedFinding]]:
    """Top findings → inline payloads (re-validated); the rest for the summary."""
    inline, overflow = [], []
    for finding in kept[: max_inline * 2]:  # consider more, post at most max
        if len(inline) >= max_inline:
            overflow.append(finding)
            continue
        if not diff.covers(finding.path, finding.start_line, finding.end_line):
            overflow.append(finding)
            continue
        inline.append(build_inline_comment(finding))
    overflow.extend(kept[max_inline * 2 :])
    return inline, overflow


def build_summary(
    walkthrough: str,
    kept: list[VerifiedFinding],
    overflow: list[VerifiedFinding],
    docs_section: str,
    cost_usd: float,
    run_url: str | None,
    warnings: list[str] | None = None,
) -> str:
    """One summary comment per PR, updated on every run (marker below)."""
    lines = [SUMMARY_MARKER, f"## Winnow review\n\n{walkthrough}\n"]
    if warnings:
        lines.extend(f"> ⚠️ {warning}\n" for warning in warnings)
    if kept or overflow:
        lines.append("| Severity | File | Finding |")
        lines.append("| --- | --- | --- |")
        for finding in [*kept, *overflow]:
            lines.append(
                f"| {finding.severity.value} | `{finding.path}:{finding.start_line}` |"
                f" {finding.title} |"
            )
        lines.append("")
    else:
        lines.append("No issues found above the threshold. 🎉\n")
    if docs_section:
        lines.append(f"<details><summary>Docs updates</summary>\n\n{docs_section}\n</details>\n")
    lines.append(
        f"<sub>Run cost ${cost_usd:.4f}" + (f" · [trace]({run_url})" if run_url else "") + "</sub>"
    )
    return sanitize_markdown("\n".join(lines))


def build_docs_section(
    inline_count: int, patches: list, changelog_entry: str | None, notes: list[str]
) -> str:
    parts = []
    if inline_count:
        parts.append(f"{inline_count} docstring suggestion(s) posted inline above.")
    for patch in patches:
        parts.append(
            f"**{patch.path}** ({patch.reason}):\n```diff\n{patch.unified_diff[:2000]}\n```"
        )
    if changelog_entry:
        parts.append(f"CHANGELOG entry:\n```\n{changelog_entry}\n```")
    parts.extend(f"_{note}_" for note in notes)
    return "\n\n".join(parts)


async def publish(
    client,
    repo: str,
    pr_number: int,
    head_sha: str,
    check_run_id: int | None,
    walkthrough: str,
    kept: list[VerifiedFinding],
    diff: ParsedDiff,
    docs_section: str,
    cost_usd: float,
    run_url: str | None = None,
    warnings: list[str] | None = None,
    max_inline: int = 10,
) -> dict:
    """Post review + summary + check run. Returns ids for the run row."""
    inline, overflow = split_inline(kept, diff, max_inline)
    summary = build_summary(
        walkthrough, kept[:max_inline], overflow, docs_section, cost_usd, run_url, warnings
    )
    posted_inline = 0
    review_id: int | None = None
    fallback_note = ""
    if inline:
        try:
            submitted = await client.post_review(repo, pr_number, head_sha, inline, "Winnow review")
            posted_inline = len(inline)
            review_id = int(submitted.get("id", 0)) or None
        except Exception as exc:
            if "422" not in str(exc):
                raise
            # GitHub rejected an anchor: post everything in the summary instead.
            fallback_note = "> ⚠️ Inline comments were rejected; all findings are listed below.\n"
            overflow = kept
    summary_id = await _upsert_summary(client, repo, pr_number, fallback_note + summary)
    if check_run_id is not None:
        await client.complete_check_run(
            repo,
            check_run_id,
            f"{len(kept)} finding(s), {posted_inline} inline, ${cost_usd:.4f}",
            details_url=run_url,
        )
    return {
        "inline_posted": posted_inline,
        "summary_comment_id": summary_id,
        "review_id": review_id,
    }


async def _upsert_summary(client, repo: str, pr_number: int, body: str) -> int:
    existing = await _find_summary(client, repo, pr_number)
    if existing is None:
        created = await client.post_issue_comment(repo, pr_number, body)
        return int(created["id"])
    await client.patch_issue_comment(repo, existing, body)
    return existing


async def _find_summary(client, repo: str, pr_number: int) -> int | None:
    """Locate our summary comment by its hidden marker (paginated scan)."""
    page = 1
    while True:
        comments = await client._request(
            "GET",
            f"/repos/{repo}/issues/{pr_number}/comments",
            params={"per_page": 100, "page": page},
        )
        if not comments:
            return None
        for comment in comments:
            if SUMMARY_MARKER in (comment.get("body") or ""):
                return int(comment["id"])
        if len(comments) < 100:
            return None
        page += 1
