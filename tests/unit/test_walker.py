"""Unit tests for the repo walker (FR-08, RAG.md §1). No network."""

from pathlib import Path

from winnow.indexing.walker import MAX_FILES, walk_repo


def _write(root: Path, rel: str, content: bytes | str = "x = 1\n") -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        target.write_text(content, encoding="utf-8")
    else:
        target.write_bytes(content)


def test_python_file_is_found_with_language(tmp_path: Path) -> None:
    _write(tmp_path, "shop/pricing.py", "def apply_discount():\n    return 1\n")
    (found,) = walk_repo(tmp_path)
    assert found.path == "shop/pricing.py"
    assert found.language == "python"
    assert not found.is_doc


def test_gitignore_is_honoured(tmp_path: Path) -> None:
    _write(tmp_path, ".gitignore", "*.log\ndist/\n")
    _write(tmp_path, "app.log", "noise")
    _write(tmp_path, "dist/bundle.js", "x")
    _write(tmp_path, "keep.py", "x = 1\n")
    assert [f.path for f in walk_repo(tmp_path)] == [".gitignore", "keep.py"]


def test_secret_denylist_never_enters_index(tmp_path: Path) -> None:
    _write(tmp_path, ".env", "KEY=abc")
    _write(tmp_path, "cert.pem", "x")
    _write(tmp_path, "ok.py", "x = 1\n")
    assert [f.path for f in walk_repo(tmp_path)] == ["ok.py"]


def test_binary_and_oversize_and_minified_are_skipped(tmp_path: Path) -> None:
    _write(tmp_path, "img.png", b"\x89PNG\x00\x01")
    _write(tmp_path, "big.py", "x = 1  # padding to exceed the size cap\n" * 6_000)  # > 200 KB
    _write(tmp_path, "mini.js", "a" * 700 + "\n")  # avg line length > 300
    _write(tmp_path, "ok.py", "x = 1\n")
    assert [f.path for f in walk_repo(tmp_path)] == ["ok.py"]


def test_skip_dirs_and_lock_suffixes(tmp_path: Path) -> None:
    _write(tmp_path, "node_modules/dep/index.js", "x")
    _write(tmp_path, ".venv/lib/a.py", "x")
    _write(tmp_path, "uv.lock", "x")
    _write(tmp_path, "ok.py", "x = 1\n")
    assert [f.path for f in walk_repo(tmp_path)] == ["ok.py"]


def test_docs_flagged_for_separate_index(tmp_path: Path) -> None:
    _write(tmp_path, "README.md", "# Hi\n")
    _write(tmp_path, "docs/guide.md", "# G\n")
    _write(tmp_path, "shop/note.md", "# N\n")
    by_path = {f.path: f for f in walk_repo(tmp_path)}
    assert by_path["README.md"].is_doc
    assert by_path["docs/guide.md"].is_doc
    assert not by_path["shop/note.md"].is_doc


def test_extra_ignore_from_repo_config(tmp_path: Path) -> None:
    _write(tmp_path, "migrations/001.py", "x = 1\n")
    _write(tmp_path, "ok.py", "x = 1\n")
    assert [f.path for f in walk_repo(tmp_path, extra_ignore=("migrations/**",))] == ["ok.py"]


def test_max_files_cap_is_enforced(tmp_path: Path) -> None:
    for i in range(5):
        _write(tmp_path, f"f{i}.py", "x = 1\n")
    assert len(walk_repo(tmp_path, max_files=3)) == 3
    assert MAX_FILES == 20_000
