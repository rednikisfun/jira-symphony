"""GitLab merge request provider."""

from __future__ import annotations

import logging
from urllib.parse import quote_plus

import httpx

log = logging.getLogger(__name__)


class GitLabProvider:
    """Creates merge requests via the GitLab REST API v4."""

    def __init__(self, token: str, base_url: str = "https://gitlab.com") -> None:
        self._base = base_url.rstrip("/") + "/api/v4"
        self._client = httpx.AsyncClient(
            headers={
                "PRIVATE-TOKEN": token,
                "Content-Type": "application/json",
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
        """Create a merge request and return its web URL."""
        project_id = quote_plus(remote)
        url = f"{self._base}/projects/{project_id}/merge_requests"
        body = {
            "source_branch": source_branch,
            "target_branch": target_branch,
            "title": title,
            "description": description,
            "remove_source_branch": True,
        }
        log.info(
            "Creating MR: %s -> %s in %s",
            source_branch, target_branch, remote,
        )
        resp = await self._client.post(url, json=body)
        resp.raise_for_status()
        mr_url = resp.json()["web_url"]
        log.info("MR created: %s", mr_url)
        return mr_url
