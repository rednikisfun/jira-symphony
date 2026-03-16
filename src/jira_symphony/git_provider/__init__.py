"""Git provider abstraction — factory and re-exports."""

from __future__ import annotations

from .base import GitProvider
from .github import GitHubProvider
from .gitlab import GitLabProvider

__all__ = ["GitProvider", "GitHubProvider", "GitLabProvider", "get_git_provider"]


def get_git_provider(
    provider: str,
    token: str,
    base_url: str = "https://gitlab.com",
) -> GitProvider:
    """Create a GitProvider instance for the given provider type."""
    if provider == "github":
        return GitHubProvider(token)
    if provider == "gitlab":
        return GitLabProvider(token, base_url)
    raise ValueError(f"Unknown git provider: {provider!r}. Use 'github' or 'gitlab'.")
