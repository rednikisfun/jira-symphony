"""Git provider protocol definition."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class GitProvider(Protocol):
    """Protocol for git hosting providers (GitHub, GitLab, etc.)."""

    async def create_pull_request(
        self,
        remote: str,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
    ) -> str:
        """Create a PR/MR and return its web URL."""
        ...

    async def close(self) -> None:
        """Close the HTTP client."""
        ...
