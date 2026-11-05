"""Unit tests for the repo map (RAG.md §5). No network."""

from winnow.indexing.chunker import count_tokens
from winnow.indexing.repomap import build_repo_map


def test_map_contains_tree_symbols_and_readme() -> None:
    text = build_repo_map(
        full_name="aredamz/winnow-demo-shop",
        sha="3f2a9c1",
        files=["shop/pricing.py", "billing/invoice.py", "tests/test_pricing.py"],
        symbols_by_file={
            "shop/pricing.py": ["apply_discount(price, rate) -> Decimal", "class Cart"],
            "billing/invoice.py": ["class Invoice: total(), lines()"],
        },
        readme_excerpt="Mini shop API built with FastAPI",
        test_file_count=14,
    )
    assert "aredamz/winnow-demo-shop @ 3f2a9c1" in text
    assert "shop/" in text and "billing/" in text
    assert "apply_discount(price, rate)" in text
    assert "14 test files" in text
    assert "Mini shop API" in text
    assert count_tokens(text) <= 8_000


def test_map_is_deterministic_and_budget_capped() -> None:
    files = [f"pkg/mod{i}.py" for i in range(600)]
    symbols = {f: [f"func_{i}()"] for i, f in enumerate(files)}
    first = build_repo_map("u/r", "sha", files, symbols)
    second = build_repo_map("u/r", "sha", list(reversed(files)), symbols)
    assert first == second
    assert count_tokens(first) <= 8_000
    assert "more files" in first
