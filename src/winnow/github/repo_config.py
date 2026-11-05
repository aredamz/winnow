"""Per-repo configuration: `.winnow.yml` (API.md §5).

Read from the PR's BASE branch so a PR cannot disable its own review. An
invalid file never blocks review: defaults apply and the summary shows a
warning naming the bad field.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class AgentsConfig(BaseModel):
    correctness: bool = True
    security: bool = True
    performance: bool = True
    maintainability: bool = True
    docs: bool = True


class ReviewConfig(BaseModel):
    severity_threshold: str = "medium"
    max_inline_comments: int = 10
    skip_drafts: bool = True
    skip_labels: list[str] = Field(default_factory=lambda: ["winnow:skip"])


class PathsConfig(BaseModel):
    ignore: list[str] = Field(default_factory=list)


class DocsConfig(BaseModel):
    targets: list[str] = Field(default_factory=lambda: ["docstrings", "readme", "changelog"])
    changelog_path: str = "CHANGELOG.md"
    docstring_style: str = "google"


class CommandsConfig(BaseModel):
    allow_public: bool = False


class RepoConfig(BaseModel):
    version: int = 1
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    review: ReviewConfig = Field(default_factory=ReviewConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    docs: DocsConfig = Field(default_factory=DocsConfig)
    commands: CommandsConfig = Field(default_factory=CommandsConfig)
    comment_language: str = "en"

    @property
    def enabled_specialists(self) -> list[str]:
        order = ("correctness", "security", "performance", "maintainability")
        flags = {
            "correctness": self.agents.correctness,
            "security": self.agents.security,
            "performance": self.agents.performance,
            "maintainability": self.agents.maintainability,
        }
        return [name for name in order if flags[name]]


def parse_repo_config(text: str) -> tuple[RepoConfig, list[str]]:
    """Parse YAML; return (config, warnings). Warnings name bad fields."""
    import yaml

    try:
        data = yaml.safe_load(text) or {}
    except Exception as exc:
        return RepoConfig(), [f".winnow.yml is not valid YAML: {exc}"]
    if not isinstance(data, dict):
        return RepoConfig(), [".winnow.yml root must be a mapping"]
    warnings = _unknown_key_warnings("", data, RepoConfig)
    try:
        return RepoConfig.model_validate(data), warnings
    except Exception as exc:
        from pydantic import ValidationError

        if isinstance(exc, ValidationError):
            for error in exc.errors():
                field = ".".join(str(part) for part in error["loc"])
                warnings.append(f".winnow.yml field ignored: {field} ({error['msg']})")
        else:
            warnings.append(f".winnow.yml invalid: {exc}")
        try:
            partial = RepoConfig.model_validate(_drop_bad_keys(data))
            return partial, warnings
        except Exception:
            return RepoConfig(), warnings


def _unknown_key_warnings(prefix: str, data: dict, model: type[BaseModel]) -> list[str]:
    """Name keys the schema does not recognise (Pydantic would ignore silently)."""
    warnings = []
    fields = model.model_fields
    for key, value in data.items():
        path = f"{prefix}{key}"
        if key not in fields:
            warnings.append(f".winnow.yml unknown field ignored: {path}")
            continue
        nested = _nested_model(fields[key].annotation)
        if nested is not None and isinstance(value, dict):
            warnings.extend(_unknown_key_warnings(f"{path}.", value, nested))
    return warnings


def _nested_model(annotation):
    import typing

    origin = typing.get_origin(annotation)
    args = [a for a in typing.get_args(annotation) if a is not type(None)]
    candidate = args[0] if origin is not None and args else annotation
    try:
        from pydantic import BaseModel as _BaseModel

        if isinstance(candidate, type) and issubclass(candidate, _BaseModel):
            return candidate
    except Exception:
        return None
    return None


def _drop_bad_keys(data: dict) -> dict:
    """Keep only known top-level keys so one bad section cannot sink the file."""
    known = set(RepoConfig.model_fields)
    return {key: value for key, value in data.items() if key in known}
