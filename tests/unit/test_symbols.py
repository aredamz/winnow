"""Unit tests for symbol extraction (FR-06, RAG.md §4.1). No network."""

from winnow.indexing.symbols import extract_symbols


def test_python_defs_methods_and_public_flag() -> None:
    text = (
        "class Cart:\n"
        "    def total(self):\n"
        "        return apply_discount(1)\n"
        "\n"
        "    def _helper(self):\n"
        "        return 0\n"
        "\n"
        "def apply_discount(price):\n"
        "    return price\n"
    )
    defs, refs = extract_symbols("shop/pricing.py", text, "python")
    by_qual = {d.qualname: d for d in defs}
    assert by_qual["Cart"].kind == "class" and by_qual["Cart"].is_public
    assert by_qual["Cart.total"].kind == "method" and by_qual["Cart.total"].is_public
    assert not by_qual["Cart._helper"].is_public
    assert by_qual["apply_discount"].kind == "function"
    assert ("apply_discount", "Cart.total") in [(r.name, r.enclosing_qualname or "") for r in refs]


def test_python_imports_count_as_references() -> None:
    defs, refs = extract_symbols(
        "a.py", "from decimal import Decimal\nimport shop.models\n", "python"
    )
    assert {r.name for r in refs} >= {"Decimal", "shop", "models"}


def test_typescript_exported_vs_private() -> None:
    text = (
        "export function add(a: number, b: number) {\n"
        "  return a + b;\n"
        "}\n"
        "function hidden() {\n"
        "  return add(1, 2);\n"
        "}\n"
    )
    defs, refs = extract_symbols("src/add.ts", text, "typescript")
    by_qual = {d.qualname: d for d in defs}
    assert by_qual["add"].is_public
    assert not by_qual["hidden"].is_public
    assert any(r.name == "add" and r.enclosing_qualname == "hidden" for r in refs)


def test_unknown_language_yields_nothing() -> None:
    assert extract_symbols("main.go", "package main\n", "go") == ([], [])
    assert extract_symbols("x.py", "(((def\n", "python") is not None
