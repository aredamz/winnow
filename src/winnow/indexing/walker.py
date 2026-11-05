"""Repository walker: file selection for indexing (RAG.md §1, FR-08).

Rules, in order:
1. Honour `.gitignore` plus caller-supplied ignores (`.winnow.yml paths.ignore`).
2. Never enter the index: secret-denylisted names (`.env*`, `*.pem`, ...).
3. Skip generated/dependency directories and lock/minified files.
4. Skip binaries (NUL byte in the first 8 KB), files over 200 KB, and files
   whose average line length exceeds 300 (likely minified).
5. Cap the walk at 20,000 files so a pathological repo cannot exhaust memory.

Only metadata is returned; file contents are read later by the chunker.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path

SKIP_DIRS = frozenset(
    {
        "node_modules",
        "vendor",
        "dist",
        "build",
        ".venv",
        "__pycache__",
        ".git",
        ".hg",
        ".svn",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
    }
)

SIZE_LIMIT_BYTES = 200 * 1024
MAX_FILES = 20_000
BINARY_PROBE_BYTES = 8 * 1024
MAX_AVG_LINE_LENGTH = 300

SECRET_DENYLIST = (
    ".env*",
    "*.pem",
    "*.key",
    "id_rsa*",
    "*.p12",
    "credentials*.json",
    "*.tfstate",
)

SKIP_SUFFIXES = (".lock", ".min.js")

DOC_PATTERNS = ("README*", "CHANGELOG*", "docs/**/*.md")

EXTENSION_LANGUAGE = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".md": "markdown",
    ".go": "go",
    ".java": "java",
    ".rs": "rust",
}


@dataclass(frozen=True)
class WalkedFile:
    path: str  # repo-relative, POSIX
    size_bytes: int
    language: str | None
    is_doc: bool


def _matches_any(name: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(name, pat) for pat in patterns)


def _is_doc(relative: str) -> bool:
    parts = relative.split("/")
    filename = parts[-1]
    if fnmatch.fnmatchcase(filename, "README*") or fnmatch.fnmatchcase(filename, "CHANGELOG*"):
        return True
    return len(parts) > 1 and parts[0] == "docs" and filename.endswith(".md")


def _load_gitignore(root: Path):
    """Compile `.gitignore` with pathspec; return None when absent/unparseable."""
    ignore_file = root / ".gitignore"
    if not ignore_file.is_file():
        return None
    try:
        import pathspec
    except ImportError:  # pragma: no cover — dependency is required in practice
        return None
    try:
        return pathspec.PathSpec.from_lines(
            "gitwildmatch", ignore_file.read_text(encoding="utf-8").splitlines()
        )
    except OSError:
        return None


def _looks_binary(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return b"\x00" in fh.read(BINARY_PROBE_BYTES)
    except OSError:
        return True  # unreadable: skip rather than crash the walk


def _avg_line_length_ok(path: Path, size_bytes: int) -> bool:
    """Reject probable minified files without reading huge files twice."""
    try:
        with path.open("rb") as fh:
            sample = fh.read(16 * 1024)
        newlines = sample.count(b"\n")
        if newlines == 0:
            return len(sample) <= MAX_AVG_LINE_LENGTH
        return len(sample) / (newlines + 1) <= MAX_AVG_LINE_LENGTH
    except OSError:
        return False


def walk_repo(
    root: str | Path,
    extra_ignore: tuple[str, ...] = (),
    max_files: int = MAX_FILES,
) -> list[WalkedFile]:
    """List indexable files under `root`, repo-relative and sorted by path."""
    root_path = Path(root)
    spec = _load_gitignore(root_path)
    found: list[WalkedFile] = []

    for dirpath_str, dirnames, filenames in _walk(root_path):
        dirpath = Path(dirpath_str)
        # Prune before descending: skipped dirs + gitignored dirs.
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        rel_dir = dirpath.relative_to(root_path).as_posix()
        if rel_dir != ".":
            dirnames[:] = [
                d
                for d in dirnames
                if not _is_ignored(spec, f"{rel_dir}/{d}/", extra_ignore, f"{rel_dir}/{d}")
            ]
        for filename in sorted(filenames):
            if len(found) >= max_files:
                return sorted(found, key=lambda f: f.path)
            rel = filename if rel_dir == "." else f"{rel_dir}/{filename}"
            entry = _classify(root_path, rel, spec, extra_ignore)
            if entry is not None:
                found.append(entry)
    return sorted(found, key=lambda f: f.path)


def _walk(root: Path):
    """os.walk wrapper kept separate so pruning logic stays testable."""
    import os

    return os.walk(root)


def _is_ignored(spec, posix_path: str, extra_ignore: tuple[str, ...], rel: str) -> bool:
    if spec is not None and spec.match_file(posix_path):
        return True
    return any(fnmatch.fnmatchcase(rel, pat) for pat in extra_ignore)


def _classify(root: Path, rel: str, spec, extra_ignore: tuple[str, ...]) -> WalkedFile | None:
    filename = rel.rsplit("/", 1)[-1]
    if _matches_any(filename, SECRET_DENYLIST):
        return None
    if rel.endswith(SKIP_SUFFIXES):
        return None
    if _is_ignored(spec, rel, extra_ignore, rel):
        return None
    full = root / rel
    try:
        size_bytes = full.stat().st_size
    except OSError:
        return None
    if size_bytes > SIZE_LIMIT_BYTES:
        return None
    if _looks_binary(full):
        return None
    if not _avg_line_length_ok(full, size_bytes):
        return None
    suffix = full.suffix.lower()
    return WalkedFile(
        path=rel,
        size_bytes=size_bytes,
        language=EXTENSION_LANGUAGE.get(suffix),
        is_doc=_is_doc(rel),
    )
