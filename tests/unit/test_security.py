"""Unit tests for redaction, sanitising and cooldowns (TESTING.md §3). No network."""

from winnow.security.limits import Cooldowns
from winnow.security.redact import contains_secret, redact_text
from winnow.security.sanitize import SUMMARY_MARKER, sanitize_markdown

AWS = "AKIAIOSFODNN7EXAMPLE"


def test_redact_aws_github_slack_and_keys() -> None:
    assert redact_text(f"key={AWS}") == "key=[REDACTED:aws-key]"
    assert "[REDACTED:github-token]" in redact_text("t=ghp_12345678901234567890")
    assert "[REDACTED:slack-token]" in redact_text("xoxb-1234567890-abcdefghij")
    header = "-----BEGIN RSA PRIVATE KEY-----"  # gitleaks:allow
    pem = f"{header}\nABC\n-----END RSA PRIVATE KEY-----"
    assert "[REDACTED:private-key]" in redact_text(pem)
    assert contains_secret(AWS) and not contains_secret("nothing here")


def test_redact_idempotent_and_safe() -> None:
    once = redact_text(f"a {AWS} b")
    assert redact_text(once) == once
    assert redact_text("") == ""


def test_sanitize_strips_images_html_and_external_links() -> None:
    dirty = (
        "see ![x](https://evil.example/?d=1)\n"
        "<script>alert(1)</script>\n"
        "[click](https://evil.example/) and [repo](https://github.com/a/b)\n"
        f"{SUMMARY_MARKER}\n"
    )
    clean = sanitize_markdown(dirty)
    assert "evil.example" not in clean
    assert "<script>" not in clean
    assert "click" in clean and "https://github.com/a/b" in clean
    assert SUMMARY_MARKER in clean


def test_sanitize_preserves_code_blocks() -> None:
    code = "```python\n![x](https://evil.example/)\n<b>hi</b>\n```\n"
    assert sanitize_markdown(code) == code
    assert "`<b>`" in sanitize_markdown("use `<b>` inline")


def test_cooldown_allows_then_blocks() -> None:
    cooldowns = Cooldowns()
    assert cooldowns.check("a/b", 1, "u")
    assert not cooldowns.check("a/b", 1, "u")
    assert cooldowns.check("a/b", 1, "other-user")
    assert cooldowns.check("a/b", 2, "u")
