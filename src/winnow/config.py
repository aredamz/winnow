"""Central configuration. The only place that reads environment variables.

CONVENTIONS.md: config only through Settings. No os.environ anywhere else.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", populate_by_name=True
    )

    # Meta Model API
    model_api_key: str = Field(default="", description="Bearer token for https://api.meta.ai/v1")
    model: str = Field(default="muse-spark-1.3", validation_alias="WINNOW_MODEL")
    llm_mode: str = Field(default="live", validation_alias="WINNOW_LLM_MODE")
    allow_contributor: bool = Field(default=False, validation_alias="WINNOW_ALLOW_CONTRIBUTOR")

    # GitHub App
    github_app_id: str = Field(default="")
    github_app_slug: str = Field(default="")
    github_app_private_key_path: str = Field(default="")
    github_webhook_secret: str = Field(default="")

    # Server / storage
    data_dir: Path = Field(default=Path("./data"), validation_alias="WINNOW_DATA_DIR")
    public_base_url: str = Field(
        default="http://localhost:8000", validation_alias="WINNOW_PUBLIC_BASE_URL"
    )
    public_repos: str = Field(default="", validation_alias="WINNOW_PUBLIC_REPOS")
    daily_spend_usd: float = Field(default=20.0, validation_alias="WINNOW_DAILY_SPEND_USD")
    admin_token: str = Field(default="", validation_alias="WINNOW_ADMIN_TOKEN")
    log_prompts: bool = Field(default=False, validation_alias="WINNOW_LOG_PROMPTS")
    log_level: str = Field(default="INFO", validation_alias="WINNOW_LOG_LEVEL")
    workers: int = Field(default=2, validation_alias="WINNOW_WORKERS")

    @property
    def public_repo_allowlist(self) -> set[str]:
        return {r.strip() for r in self.public_repos.split(",") if r.strip()}

    def public_base(self) -> str:
        return self.public_base_url.rstrip("/")


@lru_cache
def get_settings() -> Settings:
    return Settings()
