"""SQLAlchemy 2.0 models: direct port of DATA_MODEL.md §2 DDL.

The FTS5 virtual table is NOT a mapped model (SQLAlchemy has no native
support); it is created by `storage.db.init_db` and maintained explicitly by
the indexer, which deletes FTS rows whenever it deletes chunks.
"""

from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


_NOW = text("(datetime('now'))")


class Installation(Base):
    __tablename__ = "installations"

    id: Mapped[int] = mapped_column(primary_key=True)
    github_installation_id: Mapped[int] = mapped_column(unique=True)
    account_login: Mapped[str] = mapped_column(String(255))
    account_type: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[str] = mapped_column(server_default=_NOW)
    deleted_at: Mapped[str | None] = mapped_column(nullable=True)

    __table_args__ = (CheckConstraint("account_type IN ('User', 'Organization')"),)


class Repo(Base):
    __tablename__ = "repos"

    id: Mapped[int] = mapped_column(primary_key=True)
    installation_id: Mapped[int] = mapped_column(ForeignKey("installations.id", ondelete="CASCADE"))
    github_repo_id: Mapped[int] = mapped_column(unique=True)
    full_name: Mapped[str] = mapped_column(String(255))
    private: Mapped[int] = mapped_column(default=1)  # 0/1; gates web_search + contributor models
    default_branch: Mapped[str] = mapped_column(String(128))
    indexed_sha: Mapped[str | None] = mapped_column(nullable=True)
    index_status: Mapped[str] = mapped_column(String(16), default="pending")
    settings_json: Mapped[str] = mapped_column(Text, default="{}")
    public_dashboard: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[str] = mapped_column(server_default=_NOW)

    __table_args__ = (
        CheckConstraint("index_status IN ('pending', 'indexing', 'ready', 'failed')"),
    )


class File(Base):
    __tablename__ = "files"

    id: Mapped[int] = mapped_column(primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id", ondelete="CASCADE"))
    path: Mapped[str] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(String(32), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column()
    indexed_at: Mapped[str] = mapped_column(server_default=_NOW)

    __table_args__ = (Index("files_repo_path", "repo_id", "path", unique=True),)


class Chunk(Base):
    __tablename__ = "chunks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id", ondelete="CASCADE"))
    file_id: Mapped[int] = mapped_column(ForeignKey("files.id", ondelete="CASCADE"))
    symbol: Mapped[str | None] = mapped_column(Text, nullable=True)
    kind: Mapped[str] = mapped_column(String(32))
    start_line: Mapped[int] = mapped_column()
    end_line: Mapped[int] = mapped_column()
    content_hash: Mapped[str] = mapped_column(String(64))
    header: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    token_count: Mapped[int] = mapped_column()

    __table_args__ = (Index("chunks_repo_file", "repo_id", "file_id"),)


class Symbol(Base):
    __tablename__ = "symbols"

    id: Mapped[int] = mapped_column(primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id", ondelete="CASCADE"))
    file_id: Mapped[int] = mapped_column(ForeignKey("files.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(Text)
    qualname: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(32))
    line: Mapped[int] = mapped_column()
    is_public: Mapped[int] = mapped_column()  # 0/1; docs drift detection

    __table_args__ = (Index("symbols_name", "repo_id", "name"),)


class Ref(Base):
    __tablename__ = "refs"

    id: Mapped[int] = mapped_column(primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id", ondelete="CASCADE"))
    file_id: Mapped[int] = mapped_column(ForeignKey("files.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(Text)
    line: Mapped[int] = mapped_column()
    enclosing_qualname: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (Index("refs_name", "repo_id", "name"),)


class RepoMap(Base):
    __tablename__ = "repo_maps"

    repo_id: Mapped[int] = mapped_column(
        ForeignKey("repos.id", ondelete="CASCADE"), primary_key=True
    )
    sha: Mapped[str] = mapped_column(String(64), primary_key=True)
    text: Mapped[str] = mapped_column(Text)
    token_count: Mapped[int] = mapped_column()
    created_at: Mapped[str] = mapped_column(server_default=_NOW)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    repo_id: Mapped[int | None] = mapped_column(
        ForeignKey("repos.id", ondelete="CASCADE"), nullable=True
    )
    dedupe_key: Mapped[str | None] = mapped_column(Text, unique=True, nullable=True)
    payload_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="queued")
    attempts: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[str] = mapped_column(server_default=_NOW)
    started_at: Mapped[str | None] = mapped_column(nullable=True)
    finished_at: Mapped[str | None] = mapped_column(nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint("kind IN ('index', 'review', 'command', 'cleanup', 'poll_reactions')"),
        CheckConstraint("status IN ('queued', 'running', 'done', 'failed', 'cancelled')"),
    )


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"

    delivery_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event: Mapped[str] = mapped_column(String(64))
    received_at: Mapped[str] = mapped_column(server_default=_NOW)


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id", ondelete="CASCADE"))
    pr_number: Mapped[int | None] = mapped_column(nullable=True)
    base_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    head_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    trigger: Mapped[str] = mapped_column(String(16))
    mode: Mapped[str] = mapped_column(String(16), default="rag")
    status: Mapped[str] = mapped_column(String(16))
    started_at: Mapped[str] = mapped_column(server_default=_NOW)
    finished_at: Mapped[str | None] = mapped_column(nullable=True)
    tokens_in: Mapped[int] = mapped_column(default=0)
    tokens_cached: Mapped[int] = mapped_column(default=0)
    tokens_out: Mapped[int] = mapped_column(default=0)
    cost_usd: Mapped[float] = mapped_column(default=0.0)
    summary_comment_id: Mapped[int | None] = mapped_column(nullable=True)
    check_run_id: Mapped[int | None] = mapped_column(nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint("trigger IN ('webhook', 'command', 'cli', 'eval')"),
        CheckConstraint("mode IN ('rag', 'baseline', 'full')"),
        CheckConstraint("status IN ('running', 'done', 'failed', 'cancelled')"),
        Index("runs_pr", "repo_id", "pr_number"),
    )


class AgentStep(Base):
    __tablename__ = "agent_steps"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    agent: Mapped[str] = mapped_column(String(64))
    unit_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt_version: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(64))
    effort: Mapped[str | None] = mapped_column(String(16), nullable=True)
    round: Mapped[int] = mapped_column(default=0)
    tool_calls_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    tokens_in: Mapped[int] = mapped_column(default=0)
    tokens_cached: Mapped[int] = mapped_column(default=0)
    tokens_out: Mapped[int] = mapped_column(default=0)
    cost_usd: Mapped[float] = mapped_column(default=0.0)
    latency_ms: Mapped[int | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(String(16))
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[str] = mapped_column(server_default=_NOW)

    __table_args__ = (CheckConstraint("status IN ('running', 'ok', 'error', 'skipped')"),)


class Finding(Base):
    __tablename__ = "findings"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    agent: Mapped[str] = mapped_column(String(64))
    path: Mapped[str] = mapped_column(Text)
    start_line: Mapped[int] = mapped_column()
    end_line: Mapped[int] = mapped_column()
    severity: Mapped[str] = mapped_column(String(16))
    category: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(Text)
    explanation: Mapped[str] = mapped_column(Text)
    evidence_json: Mapped[str] = mapped_column(Text)
    suggestion: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float] = mapped_column()
    verdict: Mapped[str] = mapped_column(String(16))
    verifier_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    github_comment_id: Mapped[int | None] = mapped_column(nullable=True)
    posted_at: Mapped[str | None] = mapped_column(nullable=True)

    __table_args__ = (
        CheckConstraint("verdict IN ('keep', 'drop')"),
        Index("findings_fingerprint", "fingerprint"),
    )


class DocsPatch(Base):
    __tablename__ = "docs_patches"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    path: Mapped[str] = mapped_column(Text)
    unified_diff: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16))

    __table_args__ = (CheckConstraint("status IN ('proposed', 'applied', 'failed')"),)


class Feedback(Base):
    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(primary_key=True)
    finding_id: Mapped[int] = mapped_column(ForeignKey("findings.id", ondelete="CASCADE"))
    github_user: Mapped[str] = mapped_column(String(255))
    reaction: Mapped[str] = mapped_column(String(8))
    observed_at: Mapped[str] = mapped_column(server_default=_NOW)

    __table_args__ = (
        CheckConstraint("reaction IN ('+1', '-1')"),
        Index("feedback_finding_user", "finding_id", "github_user", unique=True),
    )


class SpendDaily(Base):
    __tablename__ = "spend_daily"

    day: Mapped[str] = mapped_column(String(10), primary_key=True)  # YYYY-MM-DD UTC
    cost_usd: Mapped[float] = mapped_column(default=0.0)
