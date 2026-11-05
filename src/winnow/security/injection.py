"""Prompt-injection tripwires: deterministic, LLM-free (SECURITY T1/T2).

The model rules treat <untrusted> text as data, but defence in depth notes
the attempt in the summary so humans see it (INJ-01 expectation). Patterns
are deliberately narrow to avoid flagging ordinary PR prose.
"""

from __future__ import annotations

import re

_PATTERNS = [
    re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions?", re.I),
    re.compile(r"disregard\s+(all\s+)?(previous|prior|above)\s+instructions?", re.I),
    re.compile(r"\bapprove\s+(this|the)\s+PR\b", re.I),
    re.compile(r"\breply\s+LGTM\b", re.I),
    re.compile(r"print\s+(your|the)\s+system\s+prompt", re.I),
    re.compile(r"AI\s+reviewers?\s*:", re.I),
]


def detect_instruction_attempt(*texts: str) -> bool:
    """True when PR text looks aimed at AI reviewers rather than humans."""
    combined = "\n".join(texts or "")
    return any(pattern.search(combined) is not None for pattern in _PATTERNS)
