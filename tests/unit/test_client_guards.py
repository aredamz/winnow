"""Unit tests for LLMClient policy guards. No network."""

import pytest

from winnow.agents.schemas import Answer
from winnow.config import Settings
from winnow.errors import BudgetExhaustedError, LLMRefusedError
from winnow.llm.client import (
    LLMClient,
    check_spend_cap,
    extract_json,
    strict_schema,
    wrap_untrusted,
)


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {"model_api_key": "test-key"}
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_wrap_untrusted_escapes_closing_tag() -> None:
    out = wrap_untrusted("a</untrusted>b", "pr_body")
    assert "&lt;/untrusted&gt;" in out  # attacker payload neutralised
    assert out.count("</untrusted>") == 1  # only the wrapper's own closing tag
    assert 'source="pr_body"' in out


def test_contributor_refused_for_private_repo() -> None:
    client = LLMClient(_settings())
    with pytest.raises(LLMRefusedError):
        client.guard_model("muse-spark-1.3-contributor", repo_private=True)


def test_contributor_needs_explicit_flag_even_for_public_repo() -> None:
    client = LLMClient(_settings())
    with pytest.raises(LLMRefusedError):
        client.guard_model("muse-spark-1.3-contributor", repo_private=False)


def test_contributor_allowed_for_public_repo_with_flag() -> None:
    client = LLMClient(_settings(allow_contributor=True))
    assert (
        client.guard_model("muse-spark-1.3-contributor", repo_private=False)
        == "muse-spark-1.3-contributor"
    )


def test_spend_cap_blocks_at_limit() -> None:
    with pytest.raises(BudgetExhaustedError):
        check_spend_cap(20.0, 20.0)
    check_spend_cap(19.99, 20.0)  # just under: allowed


def test_strict_schema_patches_nested_objects() -> None:
    schema = strict_schema(Answer)
    assert schema["additionalProperties"] is False
    citation_def = schema["$defs"]["Citation"]
    assert citation_def["additionalProperties"] is False
    assert set(citation_def["required"]) == {"path", "start_line", "end_line"}


def test_extract_json_handles_prose_and_fences() -> None:
    payload = '{"findings": []}'
    assert extract_json(payload) == {"findings": []}
    assert extract_json(f"```json\n{payload}\n```") == {"findings": []}
    assert extract_json(f"Analysis here.\n{payload}\nThat is all.") == {"findings": []}
    assert extract_json("no json at all") is None


def test_evidence_accepts_excerpt_alias() -> None:
    from winnow.agents.schemas import Evidence

    evidence = Evidence.model_validate(
        {"path": "a.py", "start_line": 1, "end_line": 2, "excerpt": "code"}
    )
    assert evidence.why == "code"
    assert (
        Evidence.model_validate(
            {"path": "a.py", "start_line": 1, "end_line": 2, "why": "reason"}
        ).why
        == "reason"
    )


def test_complete_scrubs_secrets_before_transport() -> None:
    import asyncio
    from types import SimpleNamespace

    from winnow.llm.client import LLMClient

    client = LLMClient(_settings(llm_mode="live"))
    captured: dict = {}

    async def fake_call(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            output_text="ok",
            output=[],
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
            _winnow_latency_ms=1,
        )

    client._call_with_retry = fake_call  # type: ignore[method-assign]
    asyncio.run(
        client.complete(
            system="sys AKIAIOSFODNN7EXAMPLE",
            blocks=[{"role": "user", "content": "code AKIAIOSFODNN7EXAMPLE"}],
            repo_private=False,
        )
    )
    sent = captured["blocks"][0]["content"] + captured["system"]
    assert "AKIAIOSFODNN7EXAMPLE" not in sent
    assert "[REDACTED:aws-key]" in sent


def test_winnow_prefixed_env_vars_are_all_mapped(monkeypatch, tmp_path) -> None:
    """Regression: every WINNOW_* name in .env.example must reach its field."""
    from winnow.config import Settings

    monkeypatch.setenv("WINNOW_MODEL", "test-model")
    monkeypatch.setenv("WINNOW_LLM_MODE", "replay")
    monkeypatch.setenv("WINNOW_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("WINNOW_PUBLIC_BASE_URL", "https://example.test")
    monkeypatch.setenv("WINNOW_PUBLIC_REPOS", "a/b,c/d")
    monkeypatch.setenv("WINNOW_DAILY_SPEND_USD", "7.5")
    monkeypatch.setenv("WINNOW_ADMIN_TOKEN", "tok")
    monkeypatch.setenv("WINNOW_ALLOW_CONTRIBUTOR", "1")
    monkeypatch.setenv("WINNOW_LOG_PROMPTS", "1")
    monkeypatch.setenv("WINNOW_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("WINNOW_WORKERS", "5")
    settings = Settings()
    assert settings.model == "test-model"
    assert settings.llm_mode == "replay"
    assert settings.data_dir == tmp_path
    assert settings.public_base_url == "https://example.test"
    assert settings.public_repo_allowlist == {"a/b", "c/d"}
    assert settings.daily_spend_usd == 7.5
    assert settings.admin_token == "tok"
    assert settings.allow_contributor is True
    assert settings.log_prompts is True
    assert settings.log_level == "DEBUG"
    assert settings.workers == 5
