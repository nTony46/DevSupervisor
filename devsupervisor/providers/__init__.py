"""Provider registry. The default is free and deterministic, deliberately."""

from ..errors import NotFound
from .base import (  # noqa: F401
    BudgetExceeded,
    Provider,
    ProviderError,
    RunOutcome,
    RunRequest,
)
from .claude_cli import ClaudeCLIProvider  # noqa: F401
from .mock import MockProvider  # noqa: F401

DEFAULT = "mock"
_PROVIDERS = {"mock": MockProvider, "claude-cli": ClaudeCLIProvider}


def resolve(name=None, allow_paid=False, **kwargs):
    """Build a provider by name.

    A paid provider requires `allow_paid=True`, so no default path, no test, and
    no accidental CLI invocation can spend money.
    """
    name = name or DEFAULT
    if name not in _PROVIDERS:
        raise NotFound(f"no provider named {name!r}; available: {sorted(_PROVIDERS)}")
    provider_class = _PROVIDERS[name]
    if provider_class.is_paid and not allow_paid:
        raise BudgetExceeded(
            f"provider {name!r} is paid; pass allow_paid=True and a budget to use it"
        )
    if provider_class.is_paid:
        kwargs.setdefault("allow_paid", True)
    return provider_class(**kwargs)


def available():
    return sorted(_PROVIDERS)
