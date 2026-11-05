"""Docs agent: drift detection, docstrings, README/CHANGELOG patches (FR-20–24).

Drift detection is deterministic (old vs new signature, docstring unchanged);
the model writes the prose. Every file patch must pass `git apply --check`
against the worktree before it is proposed (MULTI_AGENT.md graph: DV step).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import structlog

from winnow.agents.schemas import Category, DocsPatch, DocsResult
from winnow.github.diff import ParsedDiff
from winnow.indexing.chunker import chunk_file
from winnow.llm.client import LLMClient, extract_json, strict_schema, wrap_untrusted

log = structlog.get_logger()

MAX_PATCH_BYTES = 50_000


def _load_prompt() -> str:
    return (Path(__file__).resolve().parents[1] / "llm" / "prompts" / "docs_writer.md").read_text(
        encoding="utf-8"
    )


def prompt_version() -> str:
    return _load_prompt().splitlines()[0].lstrip("# ").strip()


def _git_show(repo_root: str, base: str, path: str) -> str | None:
    git = shutil.which("git")
    if git is None:
        return None
    if base.startswith("-") or path.startswith("-"):
        return None
    # Fixed argv, no shell; base ref and diff paths validated above.
    proc = subprocess.run(  # noqa: S603
        [git, "-C", repo_root, "show", f"{base}:{path}"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc.stdout if proc.returncode == 0 else None


def detect_drift(
    repo_root: str, base: str, diff: ParsedDiff, changed_symbols: list[str]
) -> list[dict[str, str]]:
    """Changed public symbols with old/new signatures and docstrings.

    Recall is deterministic (every changed public symbol is a candidate,
    whether the signature text changed or only the body did — semantic drift
    like percent→rate keeps the same signature line); precision is the docs
    model's job. `signature_changed` hints the easy cases.
    """
    candidates = []
    shorts = {s.split(".")[-1] for s in changed_symbols if not s.split(".")[-1].startswith("_")}
    for entry in diff.files:
        if entry.is_deleted or entry.is_binary:
            continue
        old_text = _git_show(repo_root, base, entry.path) if not entry.is_added else None
        try:
            new_text = Path(f"{repo_root}/{entry.path}").read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            continue
        language = _lang(entry.path)
        new_chunks = {
            c.symbol: c for c in chunk_file(entry.path, new_text, language or "unknown") if c.symbol
        }
        old_chunks = (
            {
                c.symbol: c
                for c in chunk_file(entry.path, old_text, language or "unknown")
                if c.symbol
            }
            if old_text is not None
            else {}
        )
        for qualname, chunk in new_chunks.items():
            if not shorts.intersection(qualname.split(".")):
                continue
            new_code = _code_text(chunk.text)
            new_sig = new_code.splitlines()[0] if new_code.splitlines() else ""
            new_doc = _first_docstring(chunk.text)
            old_chunk = old_chunks.get(qualname)
            old_code = _code_text(old_chunk.text) if old_chunk else ""
            old_sig = old_code.splitlines()[0] if old_code.splitlines() else ""
            old_doc = _first_docstring(old_chunk.text) if old_chunk else ""
            candidates.append(
                {
                    "path": entry.path,
                    "symbol": qualname,
                    "old_signature": old_sig,
                    "new_signature": new_sig,
                    "docstring": new_doc,
                    "old_docstring": old_doc,
                    "signature_changed": str(bool(old_sig and new_sig and old_sig != new_sig)),
                    "old_body": old_code[:1200],
                    "new_body": new_code[:1200],
                }
            )
    seen, unique = set(), []
    for candidate in candidates:
        key = (candidate["path"], candidate["symbol"])
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def _code_text(chunk_text: str) -> str:
    """Chunk body without the contextual header (first line starts with # path:)."""
    lines = chunk_text.splitlines()
    if lines and lines[0].startswith("# path:"):
        return "\n".join(lines[1:])
    return chunk_text


def _first_docstring(text: str) -> str:
    match = re.search(r'("""|\'\'\')([\s\S]*?)\1', text)
    return match.group(0) if match else ""


def _lang(path: str) -> str | None:
    suffix = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return {"py": "python", "ts": "typescript", "js": "javascript", "md": "markdown"}.get(suffix)


def readme_mentions(repo_root: str, symbol: str) -> list[str]:
    """README/docs sections mentioning a symbol (for README patch context)."""
    short = symbol.split(".")[-1]
    hits = []
    root = Path(repo_root)
    doc_dir = root / "docs"
    targets = (
        [root / "README.md", *doc_dir.glob("*.md")] if doc_dir.is_dir() else [root / "README.md"]
    )
    for target in targets:
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if short in text:
            section = _section_around(text, short)
            hits.append(f"[{target.relative_to(root)}]\n{section[:1200]}")
    return hits[:6]


def _section_around(text: str, needle: str, radius: int = 800) -> str:
    idx = text.find(needle)
    start = max(0, idx - radius)
    return text[start : idx + radius]


def validate_patch(repo_root: str, patch: DocsPatch) -> tuple[bool, str]:
    """`git apply --check`: does this patch apply to the worktree?"""
    if len(patch.unified_diff.encode()) > MAX_PATCH_BYTES:
        return False, "patch exceeds 50 KB"
    git = shutil.which("git")
    if git is None:
        return False, "git not found"
    # Fixed argv, no shell; the patch itself travels via stdin, never argv.
    proc = subprocess.run(  # noqa: S603
        [git, "-C", repo_root, "apply", "--check", "-"],
        input=patch.unified_diff,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode == 0:
        return True, "applies"
    return False, proc.stderr.strip()[:300] or "does not apply"


async def write_docs(
    client: LLMClient,
    repo_root: str,
    base: str,
    diff: ParsedDiff,
    changed_symbols: list[str],
    repo_private: bool = True,
    run_id: int | None = None,
) -> tuple[DocsResult, list[str]]:
    """Run the docs agent. Returns (result, validation notes for the summary)."""
    from winnow.agents.schemas import DocsEditResult

    candidates = detect_drift(repo_root, base, diff, changed_symbols)
    mention_blocks = []
    for candidate in candidates[:8]:
        for hit in readme_mentions(repo_root, candidate["symbol"]):
            mention_blocks.append(hit)
    ranges = []
    for entry in diff.files:
        spans = ", ".join(f"{h.new_start}-{h.new_end}" for h in entry.hunks)
        if spans:
            ranges.append(f"{entry.path}: {spans}")
    blocks = [
        {
            "role": "user",
            "content": wrap_untrusted(
                "\n\n".join(
                    f"[{c['path']}] {c['symbol']}\nold: {c['old_signature']}\n"
                    f"new: {c['new_signature']}\n"
                    f"old docstring: {c.get('old_docstring') or '(none)'}\n"
                    f"new docstring: {c['docstring'] or '(none)'}\n"
                    f"signature_changed: {c.get('signature_changed', '?')}\n"
                    f"old body:\n{c.get('old_body', '')}\n"
                    f"new body:\n{c.get('new_body', '')}"
                    for c in candidates[:12]
                )
                or "(no changed public symbols)",
                "drift_candidates",
            ),
        },
        {
            "role": "user",
            "content": wrap_untrusted(
                "Valid inline-suggestion ranges (RIGHT-side hunks):\n" + "\n".join(ranges),
                "valid_ranges",
            ),
        },
        {
            "role": "user",
            "content": wrap_untrusted(
                "\n\n".join(mention_blocks)[:8000] or "(no mentions)", "doc_mentions"
            ),
        },
    ]
    try:
        response = await client.complete(
            system=_load_prompt(),
            blocks=blocks,
            effort="medium",
            json_schema=strict_schema(DocsEditResult),
            schema_name="docs",
            prompt_version=prompt_version(),
            repo_private=repo_private,
            run_id=run_id,
            agent="docs",
        )
        parsed = extract_json(response.text) if response.parsed is None else response.parsed
        if parsed is None:
            edit_result = DocsEditResult()
        else:
            edit_result = DocsEditResult.model_validate(parsed)
    except Exception as exc:
        log.warning("docs_failed", run_id=run_id, error=str(exc)[:160])
        return DocsResult(), [f"docs agent failed: {exc}"]
    result = DocsResult(
        inline_suggestions=edit_result.inline_suggestions,
        changelog_entry=edit_result.changelog_entry,
    )
    notes: list[str] = []
    for edit in edit_result.edits:
        patch = build_patch(repo_root, edit)
        if patch is None:
            notes.append(f"{edit.path}: search block not found verbatim, skipped")
            continue
        ok, note = validate_patch(repo_root, patch)
        notes.append(f"{patch.path}: {note}")
        if ok:
            result.file_patches.append(patch)
    if candidates and not result.inline_suggestions and not result.file_patches:
        notes.append(f"model proposed no updates for {len(candidates)} drift candidate(s)")
    inline = []
    for suggestion in result.inline_suggestions:
        if suggestion.category != Category.DOCS:
            suggestion.category = Category.DOCS
        if diff.covers(suggestion.path, suggestion.start_line, suggestion.end_line):
            inline.append(suggestion)
        else:
            notes.append(f"inline docstring for {suggestion.path} outside diff: dropped")
    result.inline_suggestions[:] = inline
    return result, notes


def build_patch(repo_root: str, edit) -> DocsPatch | None:
    """Build a unified diff from a search-replace edit (difflib: exact counts).

    Returns None when old_text is absent or ambiguous (0 or 2+ matches).
    """
    import difflib

    try:
        current = Path(f"{repo_root}/{edit.path}").read_text(encoding="utf-8")
    except OSError:
        return None
    if current.count(edit.old_text) != 1:
        return None
    updated = current.replace(edit.old_text, edit.new_text)
    diff_lines = difflib.unified_diff(
        current.splitlines(keepends=True),
        updated.splitlines(keepends=True),
        fromfile=f"a/{edit.path}",
        tofile=f"b/{edit.path}",
    )
    return DocsPatch(path=edit.path, unified_diff="".join(diff_lines), reason=edit.reason)
