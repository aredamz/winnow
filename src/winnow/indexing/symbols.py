"""Symbol extraction: definitions and references (RAG.md §4.1, FR-06).

Resolution is name + import based, without type inference. Known limitation
(RAG.md): identical names in two modules can mix; the retriever mitigates by
preferring paths that import the module. This module only extracts facts;
`find_references` lives in retrieval (F1, later unit).
"""

from __future__ import annotations

from dataclasses import dataclass

from winnow.indexing.chunker import _get_parser


@dataclass(frozen=True)
class SymbolDef:
    name: str
    qualname: str
    kind: str  # function | method | class
    line: int  # 1-based
    is_public: bool


@dataclass(frozen=True)
class SymbolRef:
    name: str
    line: int  # 1-based
    enclosing_qualname: str | None


def _node_text(source: bytes, node) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


NAME_NODE_TYPES = ("identifier", "name", "property_identifier", "private_property_identifier")


def _first_identifier(node, source: bytes) -> tuple[str, int] | None:
    """First identifier-ish child: (name, 1-based line)."""
    for child in node.children:
        if child.type in NAME_NODE_TYPES:
            text = _node_text(source, child)
            return text, child.start_point[0] + 1
    for child in node.children:
        found = _first_identifier(child, source)
        if found:
            return found
    return None


def extract_symbols(
    path: str, text: str, language: str | None
) -> tuple[list[SymbolDef], list[SymbolRef]]:
    """Extract defs and refs from one file. Never raises; unknown languages yield nothing."""
    if language not in ("python", "typescript", "javascript"):
        return [], []
    parser = _get_parser(language)
    if parser is None:
        return [], []
    try:
        tree = parser.parse(text.encode("utf-8"))
    except Exception:
        return [], []
    source = text.encode("utf-8")
    defs: list[SymbolDef] = []
    refs: list[SymbolRef] = []
    if language == "python":
        _walk_python(tree.root_node, source, [], defs, refs)
    else:
        _walk_ts(tree.root_node, source, [], False, defs, refs)
    return defs, refs


def _is_public_python(name: str) -> bool:
    return not name.startswith("_")


def _walk_python(
    node, source: bytes, stack: list[tuple[str, str]], defs: list[SymbolDef], refs: list[SymbolRef]
) -> None:
    kind = node.type
    names = [name for name, _ in stack]
    if kind == "class_definition":
        found = _first_identifier(node, source)
        if found:
            name, line = found
            qualname = ".".join([*names, name])
            defs.append(SymbolDef(name, qualname, "class", line, _is_public_python(name)))
            stack = [*stack, (name, "class")]
    elif kind == "function_definition":
        found = _first_identifier(node, source)
        if found:
            name, line = found
            qualname = ".".join([*names, name])
            fn_kind = "method" if stack and stack[-1][1] == "class" else "function"
            defs.append(SymbolDef(name, qualname, fn_kind, line, _is_public_python(name)))
            stack = [*stack, (name, "function")]
    elif kind == "call":
        func = node.child_by_field_name("function")
        if func is not None:
            name = _node_text(source, func).split(".")[-1].split("(")[0].strip()
            if name.isidentifier():
                enclosing = ".".join(names) if names else None
                refs.append(SymbolRef(name, node.start_point[0] + 1, enclosing))
    elif kind in ("import_statement", "import_from_statement"):
        for child in node.children:
            if child.type == "dotted_name":
                for part in _node_text(source, child).split("."):
                    if part.isidentifier():
                        refs.append(SymbolRef(part, child.start_point[0] + 1, None))
            elif child.type in ("identifier", "aliased_import"):
                found = _first_identifier(child, source)
                if found:
                    refs.append(SymbolRef(found[0], found[1], None))
    for child in node.children:
        _walk_python(child, source, stack, defs, refs)


def _walk_ts(
    node,
    source: bytes,
    stack: list[tuple[str, str]],
    exported: bool,
    defs: list[SymbolDef],
    refs: list[SymbolRef],
) -> None:
    kind = node.type
    names = [name for name, _ in stack]
    if kind == "export_statement":
        for child in node.children:
            _walk_ts(child, source, stack, True, defs, refs)
        return
    if kind in ("class_declaration", "class"):
        found = _first_identifier(node, source)
        if found:
            name, line = found
            qualname = ".".join([*names, name])
            defs.append(SymbolDef(name, qualname, "class", line, exported))
            stack = [*stack, (name, "class")]
    elif kind == "function_declaration":
        found = _first_identifier(node, source)
        if found:
            name, line = found
            qualname = ".".join([*names, name])
            fn_kind = "method" if stack and stack[-1][1] == "class" else "function"
            defs.append(SymbolDef(name, qualname, fn_kind, line, exported))
            stack = [*stack, (name, "function")]
    elif kind == "method_definition":
        found = _first_identifier(node, source)
        if found:
            name, line = found
            qualname = ".".join([*names, name])
            defs.append(SymbolDef(name, qualname, "method", line, exported))
            stack = [*stack, (name, "function")]
    elif kind == "call_expression":
        func = node.child_by_field_name("function")
        if func is not None:
            name = _node_text(source, func).split(".")[-1].split("(")[0].strip()
            if name.isidentifier():
                refs.append(
                    SymbolRef(name, node.start_point[0] + 1, ".".join(names) if names else None)
                )
    elif kind == "import_statement":
        for child in node.children:
            if child.type in ("identifier", "identifier_name"):
                text = _node_text(source, child)
                if text.isidentifier():
                    refs.append(SymbolRef(text, child.start_point[0] + 1, None))
            elif child.type in ("named_imports", "import_clause"):
                for grand in child.children:
                    if grand.type in ("identifier", "identifier_name", "import_specifier"):
                        found = _first_identifier(grand, source)
                        if found:
                            refs.append(SymbolRef(found[0], found[1], None))
    for child in node.children:
        _walk_ts(child, source, stack, exported, defs, refs)
