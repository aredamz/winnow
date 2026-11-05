"""Agent retrieval tools: search_code, find_references, get_symbol, read_file.

Every tool reads the indexed snapshot (SQLite) or files under the repo root.
Path tools enforce the snapshot boundary (SECURITY T9): normalised paths must
stay inside the root; `..` and absolute paths are rejected with a tool error.
Results are wrapped in `<untrusted>` by the agent loop, never trusted.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from winnow.retrieval.hybrid import fts_search
from winnow.storage.models import Chunk, File, Ref, Symbol

MAX_READ_LINES = 400
MAX_SEARCH_RESULTS = 8


def _resolve(root: Path, path: str) -> Path:
    candidate = (root / path).resolve()
    if root.resolve() not in (*candidate.parents, candidate):
        raise ValueError(f"path escapes the repository: {path!r}")
    return candidate


class SnapshotTools:
    """Bound tools for one repo snapshot. Handlers map tool name → callable.

    `root` may be None (server Q&A over the index): read_file then refuses
    and points at get_symbol, so tools can never reach server disk (T9).
    """

    def __init__(self, session, repo_id: int, root: str | Path | None) -> None:
        self._session = session
        self._repo_id = repo_id
        self._root = Path(root) if root else None

    def definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "name": "search_code",
                "description": "Lexical search over indexed chunks: path, lines, excerpt.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "k": {"type": "integer"},
                    },
                    "required": ["query", "k"],
                    "additionalProperties": False,
                },
                "strict": True,
            },
            {
                "type": "function",
                "name": "find_references",
                "description": "List every place in the repository that references a symbol.",
                "parameters": {
                    "type": "object",
                    "properties": {"symbol": {"type": "string"}},
                    "required": ["symbol"],
                    "additionalProperties": False,
                },
                "strict": True,
            },
            {
                "type": "function",
                "name": "get_symbol",
                "description": "Return the full definition chunk of a symbol.",
                "parameters": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                    "additionalProperties": False,
                },
                "strict": True,
            },
            {
                "type": "function",
                "name": "read_file",
                "description": "Read line-numbered snapshot lines. At most 400 lines.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "start_line": {"type": "integer"},
                        "end_line": {"type": "integer"},
                    },
                    "required": ["path", "start_line", "end_line"],
                    "additionalProperties": False,
                },
                "strict": True,
            },
        ]

    def handlers(self) -> dict[str, Callable[[dict[str, Any]], str]]:
        return {
            "search_code": self.search_code,
            "find_references": self.find_references,
            "get_symbol": self.get_symbol,
            "read_file": self.read_file,
        }

    def search_code(self, args: dict[str, Any]) -> str:
        query = str(args.get("query", ""))[:200]
        try:
            k = max(1, min(int(args.get("k", MAX_SEARCH_RESULTS)), 12))
        except (TypeError, ValueError):
            k = MAX_SEARCH_RESULTS
        ids = fts_search(self._session, self._repo_id, query, k)
        if not ids:
            return "no matches"
        chunks = {c.id: c for c in self._session.query(Chunk).filter(Chunk.id.in_(ids)).all()}
        lines = []
        for cid in ids:
            chunk = chunks.get(cid)
            if chunk is None:
                continue
            file = self._session.get(File, chunk.file_id)
            path = file.path if file else "?"
            lines.append(f"{path}:{chunk.start_line}-{chunk.end_line} [{chunk.symbol or '-'}]")
            lines.append(chunk.text[:1200])
        return "\n".join(lines)[:4000]

    def find_references(self, args: dict[str, Any]) -> str:
        symbol = str(args.get("symbol", "")).split(".")[-1][:120]
        refs = (
            self._session.query(Ref)
            .filter(Ref.repo_id == self._repo_id, Ref.name == symbol)
            .order_by(Ref.file_id, Ref.line)
            .limit(30)
            .all()
        )
        if not refs:
            return f"no references to {symbol}"
        lines = []
        for ref in refs:
            file = self._session.get(File, ref.file_id)
            path = file.path if file else "?"
            lines.append(f"{path}:{ref.line} (in {ref.enclosing_qualname or 'module'})")
        return "\n".join(lines)

    def get_symbol(self, args: dict[str, Any]) -> str:
        name = str(args.get("name", "")).split(".")[-1][:120]
        definition = (
            self._session.query(Symbol)
            .filter(Symbol.repo_id == self._repo_id, Symbol.name == name)
            .order_by(Symbol.id)
            .first()
        )
        if definition is None:
            return f"symbol not found: {name}"
        chunk = (
            self._session.query(Chunk)
            .filter(
                Chunk.repo_id == self._repo_id,
                Chunk.file_id == definition.file_id,
                Chunk.symbol == definition.qualname,
            )
            .first()
        )
        file = self._session.get(File, definition.file_id)
        path = file.path if file else "?"
        body = chunk.text[:2000] if chunk else "(body unavailable)"
        return f"{path}:{definition.line} {definition.qualname}\n{body}"

    def read_file(self, args: dict[str, Any]) -> str:
        if self._root is None:
            return "tool error: raw file reads unavailable here; use get_symbol"
        try:
            target = _resolve(self._root, str(args.get("path", "")))
        except ValueError as exc:
            return f"tool error: {exc}"
        try:
            start = max(1, int(args.get("start_line", 1)))
            end = int(args.get("end_line", start))
        except (TypeError, ValueError):
            return "tool error: start_line/end_line must be integers"
        if end - start + 1 > MAX_READ_LINES:
            return f"tool error: at most {MAX_READ_LINES} lines"
        try:
            lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            return f"tool error: cannot read ({exc.strerror or exc})"
        picked = lines[start - 1 : end]
        numbered = [f"{start + i:4d}  {line}" for i, line in enumerate(picked)]
        return "\n".join(numbered)[:8000] or "(empty range)"
