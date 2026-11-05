"""Unit tests for the AST chunker (FR-04/FR-05, RAG.md §2). No network."""

from winnow.indexing.chunker import chunk_file


def test_python_function_becomes_one_chunk() -> None:
    text = "def apply_discount(price):\n    return price\n"
    chunks = chunk_file("shop/pricing.py", text, "python")
    assert [(c.symbol, c.kind) for c in chunks] == [("apply_discount", "function")]
    assert chunks[0].start_line == 1 and chunks[0].end_line == 2
    assert chunks[0].header.startswith("# path: shop/pricing.py | symbol: apply_discount")


def test_python_class_yields_class_plus_methods_plus_module() -> None:
    text = (
        "from decimal import Decimal\n"
        "\n"
        "RATE = Decimal('0.1')\n"
        "\n"
        "class Cart:\n"
        '    """A cart."""\n'
        "\n"
        "    def total(self):\n"
        "        return 1\n"
        "\n"
        "    def lines(self):\n"
        "        return []\n"
    )
    chunks = chunk_file("shop/pricing.py", text, "python")
    by_symbol = {c.symbol: c for c in chunks}
    assert by_symbol["Cart"].kind == "class"
    assert "A cart." in by_symbol["Cart"].text
    assert "total" in by_symbol["Cart"].text  # method list in class summary
    assert by_symbol["Cart.total"].kind == "method"
    assert by_symbol["Cart.lines"].kind == "method"
    module = [c for c in chunks if c.kind == "module"]
    assert len(module) == 1 and "RATE" in module[0].text


def test_typescript_function_chunked() -> None:
    text = "export function add(a: number, b: number): number {\n  return a + b;\n}\n"
    chunks = chunk_file("src/add.ts", text, "typescript")
    assert any(c.symbol == "add" and c.kind == "function" for c in chunks)


def test_go_uses_window_fallback_with_overlap() -> None:
    lines = [f"line {i}" for i in range(1, 131)]
    chunks = chunk_file("main.go", "\n".join(lines) + "\n", "go")
    assert [(c.start_line, c.end_line) for c in chunks] == [(1, 60), (51, 110), (101, 130)]
    assert all(c.kind == "window" for c in chunks)


def test_markdown_splits_by_heading() -> None:
    text = "# Title\n\n## API\n\nbody a\n\n## Setup\n\nbody b\n"
    chunks = chunk_file("README.md", text, "markdown")
    assert [c.symbol for c in chunks] == ["API", "Setup"]
    assert all(c.kind == "doc_section" for c in chunks)


def test_large_function_splits_with_repeated_header() -> None:
    body = "\n".join(f"    x{i} = {i}" for i in range(600))
    chunks = chunk_file("big.py", f"def huge():\n{body}\n", "python")
    assert len(chunks) > 1
    assert {c.header.splitlines()[0].split("|")[0] for c in chunks} == {"# path: big.py "}


def test_chunk_ids_are_deterministic() -> None:
    text = "def f():\n    return 1\n"
    first = [c.chunk_id for c in chunk_file("a.py", text, "python", repo_id=7)]
    second = [c.chunk_id for c in chunk_file("a.py", text, "python", repo_id=7)]
    assert first == second and all(first)


def test_garbage_input_never_raises() -> None:
    assert chunk_file("x.py", "\x00\x01\x02\n(((def\n", "python") is not None
    assert chunk_file("empty.py", "", "python") is not None
