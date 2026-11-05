"""Output sanitiser: model text → safe GitHub markdown (SECURITY T3).

Strips images, raw HTML and off-domain links; preserves code blocks (fenced
and inline) verbatim, plus our own hidden summary marker. Applied by the
publisher, the single place that talks to GitHub (deterministic, ADR-010).
"""

from __future__ import annotations

import re

SUMMARY_MARKER = "<!-- winnow:summary -->"
_ALLOWED_LINK_HOST = "github.com"

_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_HTML_TAG_RE = re.compile(r"<(?!!-- winnow:summary --)[A-Za-z/!][^>]*>")
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")
_AUTOLINK_RE = re.compile(r"<(https?://[^>]+)>")
_FENCE_RE = re.compile(r"(```[\s\S]*?```)")
_INLINE_CODE_RE = re.compile(r"(`[^`\n]+`)")


def sanitize_markdown(text: str) -> str:
    """Sanitise prose; leave fenced blocks and inline code byte-identical."""
    if not text:
        return text
    parts = _FENCE_RE.split(text)
    for i in range(0, len(parts), 2):
        spans = _INLINE_CODE_RE.split(parts[i])
        for j in range(0, len(spans), 2):
            spans[j] = _sanitize_prose(spans[j])
        parts[i] = "".join(spans)
    return "".join(parts)


def _sanitize_prose(segment: str) -> str:
    segment = _IMAGE_RE.sub("", segment)
    segment = _AUTOLINK_RE.sub(lambda m: "" if _external(m.group(1)) else m.group(1), segment)
    segment = _MD_LINK_RE.sub(
        lambda m: m.group(1) if _external(m.group(2)) else m.group(0), segment
    )
    # Strip HTML tags but keep our own summary marker.
    marker = "\ue000"  # private-use placeholder, restored below
    segment = segment.replace(SUMMARY_MARKER, marker)
    segment = _HTML_TAG_RE.sub("", segment)
    return segment.replace(marker, SUMMARY_MARKER)


def _external(url: str) -> bool:
    lowered = url.strip().lower()
    if lowered.startswith(("http://", "https://")):
        host = lowered.split("://", 1)[1].split("/", 1)[0]
        return host != _ALLOWED_LINK_HOST and not host.endswith("." + _ALLOWED_LINK_HOST)
    if lowered.startswith(("mailto:", "#")):
        return False
    return True  # relative URLs stay (same-repo anchors)
