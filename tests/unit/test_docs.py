"""Unit tests for docs drift detection and patch validation (no network)."""

import subprocess

from winnow.agents.docs import detect_drift, readme_mentions, validate_patch
from winnow.agents.schemas import DocsPatch
from winnow.github.diff import parse_diff


def _git(repo, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        timeout=30,
        env={"GIT_CONFIG_NOSYSTEM": "1", "HOME": "/nonexistent"},
    )


def _commit_all(repo, message: str) -> None:
    _git(repo, "add", "-A")
    _git(
        repo,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "commit",
        "-m",
        message,
    )


def _fixture_repo(tmp_path):
    repo = tmp_path / "demo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "shop").mkdir()
    (repo / "shop" / "pricing.py").write_text(
        'def apply_discount(price, percent):\n    """Apply percent off."""\n    return price\n',
        encoding="utf-8",
    )
    (repo / "README.md").write_text(
        "# Demo\n\n## API\n\napply_discount(price, percent) applies percent off.\n",
        encoding="utf-8",
    )
    _commit_all(repo, "base")
    (repo / "shop" / "pricing.py").write_text(
        'def apply_discount(price, rate):\n    """Apply percent off."""\n    return price\n',
        encoding="utf-8",
    )
    return repo


def test_detect_drift_signature_changed_docstring_same(tmp_path) -> None:
    repo = _fixture_repo(tmp_path)
    proc = subprocess.run(
        ["git", "-C", str(repo), "diff", "main", "--no-color"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    diff = parse_diff(proc.stdout)
    candidates = detect_drift(str(repo), "main", diff, ["apply_discount"])
    assert len(candidates) == 1
    assert candidates[0]["symbol"] == "apply_discount"
    assert "percent" in candidates[0]["old_signature"]
    assert "rate" in candidates[0]["new_signature"]


def test_readme_mentions_found(tmp_path) -> None:
    repo = _fixture_repo(tmp_path)
    hits = readme_mentions(str(repo), "apply_discount")
    assert len(hits) == 1 and "README.md" in hits[0]


def test_validate_patch_applies_and_rejects(tmp_path) -> None:
    repo = _fixture_repo(tmp_path)
    good = DocsPatch(
        path="README.md",
        unified_diff=(
            "--- a/README.md\n+++ b/README.md\n@@ -3,3 +3,3 @@\n ## API\n \n"
            "-apply_discount(price, percent) applies percent off.\n"
            "+apply_discount(price, rate) applies a fractional rate.\n"
        ),
        reason="signature changed",
    )
    ok, _ = validate_patch(str(repo), good)
    assert ok
    bad = DocsPatch(
        path="README.md",
        unified_diff="--- a/README.md\n+++ b/README.md\n@@ -99,3 +99,3 @@\n-nope\n+yep\n",
        reason="bad hunk",
    )
    assert not validate_patch(str(repo), bad)[0]


def test_build_patch_roundtrip_and_rejects(tmp_path) -> None:
    from winnow.agents.docs import build_patch
    from winnow.agents.schemas import DocsEdit

    target = tmp_path / "README.md"
    target.write_text("# Demo\n\napply_discount(price, percent) off.\n", encoding="utf-8")
    patch = build_patch(
        str(tmp_path),
        DocsEdit(
            path="README.md",
            old_text="apply_discount(price, percent) off.",
            new_text="apply_discount(price, rate) rate.",
            reason="signature changed",
        ),
    )
    assert patch is not None
    ok, _ = validate_patch(str(tmp_path), patch)
    assert ok
    missing = build_patch(
        str(tmp_path),
        DocsEdit(path="README.md", old_text="no such text", new_text="x", reason="r"),
    )
    assert missing is None
