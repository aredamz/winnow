"""Custom exceptions. Never swallow an exception silently (CONVENTIONS.md)."""


class WinnowError(Exception):
    """Base for all Winnow errors."""


class ConfigError(WinnowError):
    pass


class LLMError(WinnowError):
    pass


class LLMRefusedError(LLMError):
    """Raised when a model choice is refused on policy grounds (e.g. contributor + private repo)."""


class BudgetExhaustedError(LLMError):
    pass


class SchemaValidationError(LLMError):
    pass


class GitHubError(WinnowError):
    pass


class WebhookAuthError(GitHubError):
    pass
