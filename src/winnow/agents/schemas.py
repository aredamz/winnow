"""Pydantic contracts for every agent (MULTI_AGENT.md §3, CONVENTIONS.md).

Schemas live here and nowhere else; JSON schemas for structured output are
generated from these models and patched by `llm.strict_schema` (strict mode
demands every key required, `additionalProperties: false`).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    """Base for all agent contracts: reject unknown keys instead of dropping them.

    Verified 28 Oct: models emit habit-keys (e.g. findings-shaped docs output);
    silent ignore turned 600 tokens into an empty result. Forbid makes such
    mismatches loud (visible failure note) instead of silently empty.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Severity(StrEnum):
    CRITICAL = "critical"  # exploitable, or data loss
    HIGH = "high"  # almost certain to break in production
    MEDIUM = "medium"  # edge-case bug, real performance problem
    LOW = "low"  # maintainability; summary only by default


class Category(StrEnum):
    BUG = "bug"
    SECURITY = "security"
    PERFORMANCE = "performance"
    MAINTAINABILITY = "maintainability"
    TEST_GAP = "test_gap"
    DOCS = "docs"


class Evidence(StrictModel):
    # Models habitually emit "excerpt"; accept it as "why" (verified 28 Oct).
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    path: str
    start_line: int
    end_line: int
    why: str = Field(
        validation_alias=AliasChoices("why", "excerpt"), max_length=200
    )  # why these lines support the claim


class Finding(StrictModel):
    path: str  # a file in the diff
    start_line: int  # RIGHT-side line
    end_line: int
    severity: Severity
    category: Category
    title: str = Field(max_length=90)
    explanation: str = Field(max_length=800)
    evidence: list[Evidence] = Field(min_length=1)
    suggestion: str | None = None  # replaces exactly start_line..end_line
    confidence: float = Field(ge=0, le=1)


class VerifiedFinding(Finding):
    agent: str
    verdict: str  # keep | drop (Literal breaks some strict-schema paths; checked in code)
    verifier_note: str = Field(max_length=300)


class FindingsResult(StrictModel):
    """Top-level wrapper: strict structured output needs an object root."""

    findings: list[Finding] = Field(default_factory=list)


class VerifiedFindingsResult(StrictModel):
    findings: list[VerifiedFinding] = Field(default_factory=list)


class HunkRef(StrictModel):
    path: str
    new_start: int
    new_end: int


class ReviewUnit(StrictModel):
    id: str
    files: list[str]
    hunks: list[HunkRef]
    changed_symbols: list[str]
    focus: list[Category]


class Plan(StrictModel):
    summary: str = Field(max_length=600)  # walkthrough for the summary comment
    risk_level: str  # low | medium | high (checked in code, strict-schema-safe)
    units: list[ReviewUnit] = Field(min_length=1)
    specialists: list[str] = Field(min_length=1)  # subset of the four roles
    docs_needed: bool


class ChunkRef(StrictModel):
    chunk_id: str
    path: str
    start_line: int
    end_line: int
    reason: str  # changed | caller | callee | test | lexical | semantic


class ContextPack(StrictModel):
    unit_id: str
    chunks: list[ChunkRef] = Field(default_factory=list)
    token_count: int = 0


class DocsPatch(StrictModel):
    path: str
    unified_diff: str
    reason: str


class DocsEdit(StrictModel):
    """A search-replace edit: the model supplies content, code builds the diff.

    LLM-counted @@ hunks are unreliable (verified 28 Oct: two corrupt patches
    in a row); difflib generates exact counts deterministically instead.
    """

    path: str
    old_text: str = Field(min_length=1)
    new_text: str = Field(min_length=1)
    reason: str


class DocsEditResult(StrictModel):
    """Top-level wrapper for the docs model (strict object root)."""

    inline_suggestions: list[Finding] = Field(default_factory=list)
    edits: list[DocsEdit] = Field(default_factory=list)
    changelog_entry: str | None = None


class DocsResult(StrictModel):
    inline_suggestions: list[Finding] = Field(default_factory=list)
    file_patches: list[DocsPatch] = Field(default_factory=list)
    changelog_entry: str | None = None


class PlanResult(StrictModel):
    """Top-level wrapper for the planner (strict object root)."""

    plan: Plan


class ContextQueries(StrictModel):
    queries: list[str] = Field(default_factory=list, max_length=6)
    symbols: list[str] = Field(default_factory=list, max_length=8)


class ContextQueriesResult(StrictModel):
    """Top-level wrapper for the context agent (strict object root)."""

    context: ContextQueries = Field(default_factory=ContextQueries)


class Citation(StrictModel):
    path: str
    start_line: int
    end_line: int


class Answer(StrictModel):
    text: str
    citations: list[Citation] = Field(default_factory=list)
