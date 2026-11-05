"""Unified diff parsing and RIGHT-side line mapping (PLAN.md F2, FR-10).

All review comments anchor to RIGHT-side (new file) lines that exist in the
diff; the verifier drops anything else, so GitHub never returns 422.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field

from winnow.indexing.chunker import chunk_file


@dataclass
class Hunk:
    old_start: int
    old_end: int
    new_start: int
    new_end: int
    added_lines: list[int] = field(default_factory=list)
    text: str = ""


@dataclass
class ParsedFile:
    path: str
    old_path: str
    is_added: bool = False
    is_deleted: bool = False
    is_rename: bool = False
    is_binary: bool = False
    hunks: list[Hunk] = field(default_factory=list)

    @property
    def right_lines(self) -> set[int]:
        """Every new-file line covered by a hunk (added + context)."""
        covered: set[int] = set()
        for hunk in self.hunks:
            covered.update(range(hunk.new_start, hunk.new_end + 1))
        return covered


@dataclass
class ParsedDiff:
    files: list[ParsedFile] = field(default_factory=list)

    def file_map(self) -> dict[str, ParsedFile]:
        return {f.path: f for f in self.files}

    def covers(self, path: str, start: int, end: int) -> bool:
        """True when start..end sits inside RIGHT-side hunks of path (FR-10)."""
        target = self.file_map().get(path)
        if target is None or target.is_deleted:
            return False
        covered = target.right_lines
        return all(line in covered for line in range(start, end + 1))

    @property
    def total_added(self) -> int:
        return sum(len(h.added_lines) for f in self.files for h in f.hunks)


def parse_diff(diff_text: str) -> ParsedDiff:
    """Parse a unified diff. Never raises on garbage; returns what it can."""
    import structlog
    from unidiff.patch import PatchSet

    try:
        patch = PatchSet(diff_text)
    except Exception as exc:
        # Atomic parse: one malformed hunk discards the whole diff. Git
        # generates well-formed diffs; anything else is logged, not trusted.
        structlog.get_logger().warning("diff_parse_failed", error=str(exc)[:160])
        return ParsedDiff()
    parsed = ParsedDiff()
    for patched in patch:
        if patched.is_binary_file:
            parsed.files.append(
                ParsedFile(path=patched.path, old_path=patched.source_file, is_binary=True)
            )
            continue
        entry = ParsedFile(
            path=patched.path,
            old_path=(
                patched.source_file.lstrip("a/") if not patched.is_added_file else patched.path
            ),
            is_added=patched.is_added_file,
            is_deleted=patched.is_removed_file,
            is_rename=patched.is_rename,
        )
        for hunk in patched:
            lines = list(hunk)
            added = [ln.target_line_no for ln in lines if ln.is_added and ln.target_line_no]
            entry.hunks.append(
                Hunk(
                    old_start=hunk.source_start,
                    old_end=hunk.source_start + max(0, hunk.source_length - 1),
                    new_start=hunk.target_start,
                    new_end=hunk.target_start + max(0, hunk.target_length - 1),
                    added_lines=added,
                    text=str(hunk),
                )
            )
        parsed.files.append(entry)
    return parsed


def apply_size_caps(diff: ParsedDiff, max_files: int = 50, max_lines: int = 3000) -> list[str]:
    """Truncate oversized diffs with a coverage note (NFR-10, LIM-01)."""
    notes = []
    if len(diff.files) > max_files or diff.total_added > max_lines:
        notes.append(
            f"Large change ({len(diff.files)} files, {diff.total_added} added lines): "
            f"partial review of the first {max_files} files."
        )
        diff.files = diff.files[:max_files]
    return notes


def changed_symbols_for_file(current_text: str, language: str | None, added: set[int]) -> list[str]:
    """Chunk symbols whose ranges touch an added line (hunk → symbol map)."""
    if not added:
        return []
    symbols = []
    for chunk in chunk_file("diff-target", current_text, language or "unknown"):
        if chunk.symbol and any(chunk.start_line <= line <= chunk.end_line for line in added):
            symbols.append(chunk.symbol)
    return list(dict.fromkeys(symbols))


def worktree_diff(repo_root: str, base: str) -> str:
    """Unified diff of the working tree (staged + unstaged) against `base`.

    Six context lines (not three): docstring suggestions must anchor inside a
    hunk to be postable (FR-21), and docstrings usually sit a few lines above
    the changed code. Cost impact is bounded by prompt truncation.
    """
    import shutil

    if base.startswith("-"):
        raise ValueError(f"refusing suspicious base ref: {base!r}")
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git executable not found")
    # Fixed argv, no shell; base is validated above, repo_root is the CLI path.
    proc = subprocess.run(  # noqa: S603
        [git, "-C", repo_root, "diff", "--unified=6", base, "--no-color", "--no-ext-diff"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git diff failed: {proc.stderr.strip()[:200]}")
    return proc.stdout
