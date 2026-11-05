"""FR-30: all LLM calls go through LLMClient. No other LLM endpoints in code.

Runs in CI with no network (TESTING.md §4, test_llm_client_base_url).
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

ALLOWED_FILES = {
    "src/winnow/llm/client.py",
    # probe only; chat transport still via LLMClient, embeddings check via httpx
    "scripts/smoke_muse.py",
    "tests/unit/test_llm_single_door.py",
    "tests/integration/test_llm_base_url.py",
}

# SDK / client markers that indicate a direct LLM call outside the client.
# (The base-URL string itself may appear in descriptions; the companion test
# pins client.py as the single place that defines it.)
BANNED_PATTERNS = ("AsyncOpenAI(", "OpenAI(", "anthropic", "ChatCompletion")


def test_no_direct_llm_calls_outside_client() -> None:
    offenders: list[str] = []
    for path in list((REPO / "src").rglob("*.py")) + [(REPO / "scripts" / "smoke_muse.py")]:
        if not path.exists():
            continue
        rel = path.relative_to(REPO).as_posix()
        if rel in ALLOWED_FILES:
            continue
        text = path.read_text()
        if any(p in text for p in BANNED_PATTERNS):
            offenders.append(rel)
    assert not offenders, f"direct LLM usage outside LLMClient: {offenders}"


def test_client_points_at_meta_api() -> None:
    text = (REPO / "src" / "winnow" / "llm" / "client.py").read_text()
    assert "https://api.meta.ai/v1" in text
