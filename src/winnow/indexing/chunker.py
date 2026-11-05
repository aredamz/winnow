"""AST chunker: one chunk per function/class/method (RAG.md §2, FR-04/FR-05).

- Python: `function_definition`, `class_definition` (signature + docstring +
  method list), each method.
- TypeScript/JavaScript: `function_declaration`, `class_declaration`,
  `method_definition`, exported arrow functions.
- Other languages: 60-line windows with 10-line overlap.
- Markdown: one chunk per `##`/`###` heading.
- Chunks over ~1,500 tokens split at blank-line boundaries, header repeated.
- Top-level statements (imports, constants) grouped into a module chunk.
- Every chunk carries a contextual header (path, symbol, lines, imports).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

import structlog

log = structlog.get_logger()

MAX_CHUNK_TOKENS = 1_500
WINDOW_LINES = 60
WINDOW_OVERLAP = 10

PYTHON_QUERY = """
(function_definition) @func
(class_definition) @class
"""

TS_QUERY = """
(function_declaration) @func
(class_declaration) @class
(method_definition) @method
"""


@dataclass
class Chunk:
    path: str
    language: str
    symbol: str | None
    kind: str  # function | method | class | module | window | doc_section
    start_line: int  # 1-based, inclusive
    end_line: int  # 1-based, inclusive
    header: str
    text: str
    token_count: int = 0
    chunk_id: str = ""


def count_tokens(text: str) -> int:
    """Approximate token count (tiktoken cl100k). Falls back to chars/4."""
    try:
        import tiktoken

        return len(tiktoken.get_encoding("cl100k_base").encode(text))
    except Exception:  # pragma: no cover — tiktoken is a hard dependency
        return max(1, len(text) // 4)


def make_header(path: str, symbol: str | None, start: int, end: int, imports: str) -> str:
    lines = [f"# path: {path}"]
    if symbol:
        lines[0] += f" | symbol: {symbol}"
    lines[0] += f" | lines {start}-{end}"
    if imports:
        lines.append(f"# imports: {imports}")
    return "\n".join(lines)


def chunk_id_for(repo_id: int | str, path: str, symbol: str | None, text: str) -> str:
    # sha1 is a content ID here (DATA_MODEL.md), not a security control.
    content_hash = hashlib.sha1(text.encode("utf-8")).hexdigest()  # noqa: S324
    full = f"{repo_id}{path}{symbol}{content_hash}".encode()
    return hashlib.sha1(full).hexdigest()  # noqa: S324


def _finalize(
    chunks: list[Chunk],
    path: str,
    language: str,
    symbol: str | None,
    kind: str,
    start: int,
    end: int,
    text: str,
    imports: str,
    repo_id: int | str,
) -> None:
    """Append a chunk, splitting oversized text at blank lines with header repeat."""
    parts = _split_oversized(text)
    span = end - start + 1
    for i, part in enumerate(parts):
        # Pro-rate line numbers across parts (approximate; start stays exact).
        part_start = start if i == 0 else start + (span * i) // len(parts)
        part_end = end if i == len(parts) - 1 else start + (span * (i + 1)) // len(parts) - 1
        header = make_header(path, symbol, part_start, part_end, imports)
        body = part if part.endswith("\n") else part + "\n"
        full = header + "\n" + body
        chunks.append(
            Chunk(
                path=path,
                language=language,
                symbol=symbol,
                kind=kind,
                start_line=part_start,
                end_line=part_end,
                header=header,
                text=full,
                token_count=count_tokens(full),
                chunk_id=chunk_id_for(repo_id, path, symbol, full),
            )
        )


def _split_oversized(text: str) -> list[str]:
    if count_tokens(text) <= MAX_CHUNK_TOKENS:
        return [text]
    blocks = re.split(r"\n\s*\n", text)
    parts, current = [], ""
    for block in blocks:
        candidate = f"{current}\n\n{block}" if current else block
        if count_tokens(candidate) > MAX_CHUNK_TOKENS and current:
            parts.append(current)
            current = block
        else:
            current = candidate
    if current:
        parts.append(current)
    # Dense code with no blank lines still must respect the cap: split by lines.
    final: list[str] = []
    for part in parts:
        if count_tokens(part) <= MAX_CHUNK_TOKENS:
            final.append(part)
            continue
        lines = part.splitlines()
        groups = max(2, (count_tokens(part) + MAX_CHUNK_TOKENS - 1) // MAX_CHUNK_TOKENS)
        per_group = max(1, (len(lines) + groups - 1) // groups)
        for i in range(0, len(lines), per_group):
            final.append("\n".join(lines[i : i + per_group]))
    return final or [text]


def _get_parser(language: str):
    try:
        import tree_sitter_language_pack as tslp

        get_parser = getattr(tslp, "get_parser", None)
        if get_parser is not None:
            return get_parser(language)
    except Exception:  # pragma: no cover — parser missing means fallback
        return None
    return None


def _query_captures(parser, language: str, source: bytes):
    """Return (funcs, methods, classes) node lists; empty lists on any failure."""
    from tree_sitter import Query, QueryCursor

    tree = parser.parse(source)
    query_text = PYTHON_QUERY if language == "python" else TS_QUERY
    try:
        query = Query(parser.language, query_text)
    except Exception:
        return [], [], []
    try:
        captures = QueryCursor(query).captures(tree.root_node)
    except Exception:
        return [], [], []
    # captures() returns dict[str, list[Node]] in recent tree-sitter.
    funcs = list(captures.get("func", []))
    methods = list(captures.get("method", []))
    classes = list(captures.get("class", []))
    return funcs, methods, classes


def _node_text(source: bytes, node) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _python_imports(source_text: str) -> str:
    imports = []
    for line in source_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("import ") or stripped.startswith("from "):
            imports.append(stripped)
    return ", ".join(imports[:8])


def chunk_file(path: str, text: str, language: str | None, repo_id: int | str = 0) -> list[Chunk]:
    """Split one file into chunks (FR-04/FR-05). Never raises on weird input."""
    language = language or "unknown"
    lines = text.splitlines()
    if language == "markdown":
        return _chunk_markdown(path, text, repo_id)
    if language in ("python", "typescript", "javascript"):
        try:
            return _chunk_tree_sitter(path, text, language, repo_id)
        except Exception as exc:  # fallback below; tree-sitter must never break indexing
            log.debug("chunker_fallback", path=path, language=language, error=str(exc)[:120])
    _ = lines
    return _chunk_window(path, text, language, repo_id)


def _chunk_tree_sitter(path: str, text: str, language: str, repo_id: int | str) -> list[Chunk]:
    source = text.encode("utf-8")
    parser = _get_parser(language)
    if parser is None:
        return _chunk_window(path, text, language, repo_id)
    funcs, methods, classes = _query_captures(parser, language, source)
    if not funcs and not methods and not classes:
        return _chunk_window(path, text, language, repo_id)

    chunks: list[Chunk] = []
    imports = _python_imports(text) if language == "python" else ""
    covered: list[tuple[int, int]] = []

    method_lines: set[int] = set()
    for node in methods:
        for ln in range(node.start_point[0] + 1, node.end_point[0] + 2):
            method_lines.add(ln)

    for node in classes:
        name = _child_name(node, source)
        symbol = name
        snippet = _node_text(source, node)
        first_line = snippet.splitlines()[0] if snippet else ""
        doc = _first_docstring(node, source)
        method_names = _child_method_names(node, source)
        summary = first_line
        if doc:
            summary += f"\n{doc}"
        if method_names:
            summary += "\n# methods: " + ", ".join(method_names)
        _finalize(
            chunks,
            path,
            language,
            symbol,
            "class",
            node.start_point[0] + 1,
            node.end_point[0] + 1,
            summary,
            imports,
            repo_id,
        )
        covered.append((node.start_point[0] + 1, node.end_point[0] + 1))

    for node in funcs + methods:
        start, end = node.start_point[0] + 1, node.end_point[0] + 1
        parent = _enclosing_class(node, classes, source)
        # Python methods arrive as function_definition inside a class body;
        # TS/JS arrive via the @method capture. Either way, a function with
        # an enclosing class is a method.
        is_method = (node in methods) or (parent is not None)
        if language == "python" and start in method_lines and not is_method:
            continue  # already emitted via the method list
        name = _child_name(node, source)
        symbol = f"{parent}.{name}" if is_method and parent else name
        kind = "method" if is_method else "function"
        _finalize(
            chunks,
            path,
            language,
            symbol,
            kind,
            start,
            end,
            _node_text(source, node),
            imports,
            repo_id,
        )
        covered.append((start, end))

    covered_sorted = sorted(covered)
    module_lines = [
        (i + 1, line)
        for i, line in enumerate(text.splitlines())
        if not any(s <= i + 1 <= e for s, e in covered_sorted) and line.strip()
    ]
    if module_lines:
        body = "\n".join(ln for _, ln in module_lines) + "\n"
        _finalize(
            chunks,
            path,
            language,
            None,
            "module",
            module_lines[0][0],
            module_lines[-1][0],
            body,
            imports,
            repo_id,
        )
    chunks.sort(key=lambda c: c.start_line)
    return chunks


def _child_name(node, source: bytes) -> str:
    for child in node.children:
        if child.type in ("identifier", "name", "property_identifier"):
            return _node_text(source, child)
    # TS: name may hide under declarator / formal wrapper.
    for child in node.children:
        found = _deep_name(child, source)
        if found:
            return found
    return "?"


def _deep_name(node, source: bytes, depth: int = 0) -> str | None:
    if depth > 3:
        return None
    if node.type in ("identifier", "name", "property_identifier"):
        return _node_text(source, node)
    for child in node.children:
        found = _deep_name(child, source, depth + 1)
        if found:
            return found
    return None


def _child_method_names(class_node, source: bytes) -> list[str]:
    names = []
    for child in class_node.children:
        if child.type == "block":
            for stmt in child.children:
                if stmt.type == "function_definition":
                    names.append(_child_name(stmt, source))
    return names


def _first_docstring(node, source: bytes) -> str:
    text = _node_text(source, node)
    match = re.search(r'("""|\'\'\')([\s\S]*?)\1', text)
    if match:
        return '"""' + match.group(2).strip() + '"""'
    return ""


def _enclosing_class(node, classes, source: bytes) -> str | None:
    ns, ne = node.start_byte, node.end_byte
    for cls in classes:
        if cls.start_byte <= ns and ne <= cls.end_byte and cls != node:
            return _child_name(cls, source)
    return None


def _chunk_window(path: str, text: str, language: str, repo_id: int | str) -> list[Chunk]:
    lines = text.splitlines()
    chunks: list[Chunk] = []
    step = WINDOW_LINES - WINDOW_OVERLAP
    for start0 in range(0, max(1, len(lines)), step):
        window = lines[start0 : start0 + WINDOW_LINES]
        if not window:
            break
        body = "\n".join(window) + "\n"
        _finalize(
            chunks,
            path,
            language,
            None,
            "window",
            start0 + 1,
            start0 + len(window),
            body,
            "",
            repo_id,
        )
        if start0 + WINDOW_LINES >= len(lines):
            break
    if not chunks:
        _finalize(chunks, path, language, None, "window", 1, 1, text + "\n", "", repo_id)
    return chunks


def _chunk_markdown(path: str, text: str, repo_id: int | str) -> list[Chunk]:
    chunks: list[Chunk] = []
    current_title: str | None = None
    current_lines: list[str] = []
    current_start = 1
    seen_section = False
    for i, line in enumerate(text.splitlines(), start=1):
        heading = re.match(r"^(#{1,3})\s+(.*)", line)
        level = len(heading.group(1)) if heading else 0
        if level >= 2 and seen_section:
            body = "\n".join(current_lines) + "\n"
            _finalize(
                chunks,
                path,
                "markdown",
                current_title,
                "doc_section",
                current_start,
                i - 1,
                body,
                "",
                repo_id,
            )
            current_lines, current_start = [], i
        if heading:
            # An h1 only supplies the document title, never a section split
            # (RAG.md splits on ## / ###). Prologue lines stay attached to
            # the first section instead of becoming a title-less chunk.
            if current_title is None or level >= 2:
                current_title = heading.group(2).strip()
            if level >= 2:
                seen_section = True
            if not current_lines:
                current_start = i
        current_lines.append(line)
    if current_lines:
        body = "\n".join(current_lines) + "\n"
        _finalize(
            chunks,
            path,
            "markdown",
            current_title,
            "doc_section",
            current_start,
            len(text.splitlines()),
            body,
            "",
            repo_id,
        )
    return chunks


# Re-exported for tests that patch parser availability.
__all__ = ["Chunk", "chunk_file", "count_tokens", "make_header", "chunk_id_for"]
