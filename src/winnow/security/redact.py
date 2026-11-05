"""Secret redaction: detect → [REDACTED:<type>] BEFORE the model (SECURITY T4).

The security specialist still reports "hardcoded secret" using the marker as
evidence; the value itself never reaches the Meta Model API or any comment.
Patterns are deterministic regexes (fast, testable). detect-secrets baseline
integration is an F6 hardening item; these patterns cover the test suite.
"""

from __future__ import annotations

import re

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("aws-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    (
        "aws-secret",
        re.compile(
            r"(?i)(aws_secret_access_key|aws_secret)\s*[:=]\s*['\"]?[A-Za-z0-9/+=]{32,}['\"]?"
        ),
    ),
    (
        "github-token",
        re.compile(r"\b(ghp_[A-Za-z0-9]{20,}|gho_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    ),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    (
        "private-key",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    ),
    ("generic-high-entropy", re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b")),
]


def redact_text(text: str) -> str:
    """Replace detected secrets with markers. Idempotent, never raises."""
    if not text:
        return text
    redacted = text
    for label, pattern in _PATTERNS:
        redacted = pattern.sub(f"[REDACTED:{label}]", redacted)
    return redacted


def contains_secret(text: str) -> bool:
    """True when any pattern matches (output scanning, T3)."""
    return any(pattern.search(text or "") is not None for _, pattern in _PATTERNS)
