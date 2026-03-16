"""GitHub pull request provider."""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)


class GitHubProvider:
    """Creates pull requests via the GitHub REST API."""

    def __init__(self, token: str) -> None:
        self._client = httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def create_pull_request(
        self,
        remote: str,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
    ) -> str:
        """Create a pull request and return its HTML URL."""
        url = f"https://api.github.com/repos/{remote}/pulls"
        body = {
            "head": source_branch,
            "base": target_branch,
            "title": title,
            "body": description,
        }
        log.info(
            "Creating PR: %s -> %s in %s",
            source_branch, target_branch, remote,
        )
        resp = await self._client.post(url, json=body)
        resp.raise_for_status()
        pr_url = resp.json()["html_url"]
        log.info("PR created: %s", pr_url)
        return pr_url
