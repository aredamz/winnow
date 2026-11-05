"""Repository map: stable ≤8k-token summary of a repo (RAG.md §5).

The map sits right after every agent's system prompt. Because it is identical
for every call within 24 hours, it is billed at the cached rate. Generation is
deterministic (sorted paths and symbols) so the prefix stays cache-friendly.
"""

from __future__ import annotations

from winnow.indexing.chunker import count_tokens

MAP_TOKEN_BUDGET = 8_000
MAX_FILES_IN_MAP = 400
MAX_SYMBOLS_PER_FILE = 12


def build_repo_map(
    full_name: str,
    sha: str,
    files: list[str],
    symbols_by_file: dict[str, list[str]],
    readme_excerpt: str = "",
    test_file_count: int = 0,
) -> str:
    """Assemble the map. Pure function; storage layer persists the result."""
    tree = _render_tree(sorted(files))
    sections = [f"{full_name} @ {sha}", tree]
    documented = 0
    for path in sorted(files):
        if documented >= MAX_FILES_IN_MAP:
            sections.append(f"... and {len(files) - documented} more files")
            break
        symbols = symbols_by_file.get(path, [])[:MAX_SYMBOLS_PER_FILE]
        if symbols:
            sections.append(f"{path}  {'; '.join(symbols)}")
            documented += 1
    if test_file_count:
        sections.append(f"tests: {test_file_count} test files")
    if readme_excerpt:
        sections.append(f'README: "{readme_excerpt[:300]}"')
    text = "\n".join(section for section in sections if section)
    return _truncate_to_budget(text)


def _render_tree(paths: list[str]) -> str:
    """Minimal directory tree, directories only (files appear with symbols)."""
    dirs: set[str] = set()
    for path in paths:
        parts = path.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            dirs.add("/".join(parts[:i]))
    lines = []
    for directory in sorted(dirs):
        depth = directory.count("/")
        lines.append(f"{'  ' * depth}├── {directory.split('/')[-1]}/")
    return "\n".join(lines)


def _truncate_to_budget(text: str) -> str:
    if count_tokens(text) <= MAP_TOKEN_BUDGET:
        return text
    lines = text.splitlines()
    lines = lines[: max(1, int(len(lines) * MAP_TOKEN_BUDGET / max(1, count_tokens(text))))]
    lines.append("... (truncated to 8k token budget)")
    return "\n".join(lines)
