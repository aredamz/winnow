"""Unit tests for snapshot tool guards (SECURITY T9, no network)."""

from winnow.retrieval.tools import SnapshotTools


def _tools(tmp_path, session=None, repo_id=1) -> SnapshotTools:
    (tmp_path / "ok.py").write_text("line1\nline2\nline3\n", encoding="utf-8")
    return SnapshotTools(session, repo_id, tmp_path)


def test_read_file_rejects_escape(tmp_path) -> None:
    tools = _tools(tmp_path)
    assert "escapes" in tools.read_file(
        {"path": "../../etc/passwd", "start_line": 1, "end_line": 2}
    )
    assert "escapes" in tools.read_file({"path": "/etc/passwd", "start_line": 1, "end_line": 2})


def test_read_file_ranges_and_caps(tmp_path) -> None:
    tools = _tools(tmp_path)
    out = tools.read_file({"path": "ok.py", "start_line": 2, "end_line": 3})
    assert "2  line2" in out and "3  line3" in out
    assert "at most 400" in tools.read_file({"path": "ok.py", "start_line": 1, "end_line": 500})
    assert "cannot read" in tools.read_file({"path": "missing.py", "start_line": 1, "end_line": 2})


def test_tool_registry_lists_four_tools(tmp_path) -> None:
    tools = _tools(tmp_path)
    assert set(tools.handlers()) == {
        "search_code",
        "find_references",
        "get_symbol",
        "read_file",
    }
    assert len(tools.definitions()) == 4
