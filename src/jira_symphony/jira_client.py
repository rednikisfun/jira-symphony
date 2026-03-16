"""Jira Cloud REST API client."""

from __future__ import annotations

import base64
import logging

import httpx

import re

from .config import SymphonyConfig
from .models import JiraIssue, _extract_text as _extract_comment_text

log = logging.getLogger(__name__)


class JiraClient:
    """Async Jira Cloud REST API v3 client."""

    def __init__(self, config: SymphonyConfig) -> None:
        self._cfg = config.jira
        self._base = f"https://api.atlassian.com/ex/jira/{self._cfg.cloud_id}/rest/api/3"
        creds = base64.b64encode(
            f"{self._cfg.email}:{self._cfg.api_token}".encode()
        ).decode()
        self._headers = {
            "Authorization": f"Basic {creds}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        self._client = httpx.AsyncClient(headers=self._headers, timeout=30.0)

    async def close(self) -> None:
        await self._client.aclose()

    def _build_jql(self) -> str:
        """Build JQL from configured filters."""
        f = self._cfg.filters
        parts = [f"project = {self._cfg.project_key}"]

        if f.statuses:
            status_list = ", ".join(f'"{s}"' for s in f.statuses)
            parts.append(f"status in ({status_list})")

        if f.reporter_account_ids:
            ids = ", ".join(f'"{rid}"' for rid in f.reporter_account_ids)
            parts.append(f"reporter in ({ids})")

        if f.assignee_account_ids:
            ids = ", ".join(f'"{aid}"' for aid in f.assignee_account_ids)
            parts.append(f"assignee in ({ids})")

        return " AND ".join(parts) + " ORDER BY priority DESC, created ASC"

    async def poll_todo_issues(self) -> list[JiraIssue]:
        """Fetch all To Do issues matching filters."""
        jql = self._build_jql()
        fields = [
            "summary", "description", "issuetype", "labels",
            "priority", "reporter", "assignee", "parent",
        ]
        url = f"{self._base}/search/jql"
        body = {"jql": jql, "fields": fields, "maxResults": 50}

        log.debug("Polling Jira: %s", jql)
        resp = await self._client.post(url, json=body)
        resp.raise_for_status()
        data = resp.json()

        issues = [JiraIssue.from_api(raw) for raw in data.get("issues", [])]
        log.info("Polled %d To Do issues", len(issues))

        for issue in issues:
            issue.comments = await self._fetch_comments(issue.key)

        return issues

    async def get_issue(self, issue_key: str) -> JiraIssue:
        """Fetch a single issue by key."""
        url = f"{self._base}/issue/{issue_key}"
        params = {
            "fields": "summary,description,issuetype,labels,priority,reporter,assignee,parent",
        }
        resp = await self._client.get(url, params=params)
        resp.raise_for_status()
        issue = JiraIssue.from_api(resp.json())
        issue.comments = await self._fetch_comments(issue_key)
        return issue

    async def _fetch_comments(self, issue_key: str) -> list[str]:
        """Fetch comment bodies for an issue."""
        url = f"{self._base}/issue/{issue_key}/comment"
        try:
            resp = await self._client.get(url, params={"maxResults": 20, "orderBy": "-created"})
            resp.raise_for_status()
            data = resp.json()
            comments = []
            for c in data.get("comments", []):
                body = c.get("body")
                text = _extract_comment_text(body)
                if text:
                    comments.append(text)
            return comments
        except Exception:
            log.warning("Failed to fetch comments for %s", issue_key)
            return []

    async def transition_issue(self, issue_key: str, transition_id: str) -> None:
        """Transition an issue to a new status."""
        url = f"{self._base}/issue/{issue_key}/transitions"
        body = {"transition": {"id": transition_id}}
        log.info("Transitioning %s via transition %s", issue_key, transition_id)
        resp = await self._client.post(url, json=body)
        resp.raise_for_status()

    async def add_comment(self, issue_key: str, body_text: str) -> None:
        """Add an ADF comment to an issue. Converts markdown-like text to ADF."""
        url = f"{self._base}/issue/{issue_key}/comment"
        adf_body = {"body": _text_to_adf(body_text)}
        log.info("Commenting on %s", issue_key)
        resp = await self._client.post(url, json=adf_body)
        resp.raise_for_status()

    async def add_label(self, issue_key: str, label: str) -> None:
        """Add a label to an issue."""
        url = f"{self._base}/issue/{issue_key}"
        body = {"update": {"labels": [{"add": label}]}}
        resp = await self._client.put(url, json=body)
        resp.raise_for_status()

    async def test_connection(self) -> dict:
        """Test credentials with GET /myself. Returns user info."""
        url = f"{self._base}/myself"
        resp = await self._client.get(url)
        resp.raise_for_status()
        return resp.json()


_LINK_RE = re.compile(r"(https?://\S+)")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_CODE_RE = re.compile(r"`([^`]+)`")


def _text_to_adf(text: str) -> dict:
    """Convert markdown-like text to Atlassian Document Format."""
    lines = text.split("\n")
    content: list[dict] = []

    i = 0
    while i < len(lines):
        line = lines[i]

        # Headings
        if line.startswith("### "):
            content.append({
                "type": "heading", "attrs": {"level": 3},
                "content": _inline_nodes(line[4:]),
            })
        elif line.startswith("## "):
            content.append({
                "type": "heading", "attrs": {"level": 2},
                "content": _inline_nodes(line[3:]),
            })
        elif line.startswith("# "):
            content.append({
                "type": "heading", "attrs": {"level": 1},
                "content": _inline_nodes(line[2:]),
            })
        # Bullet list items — collect consecutive
        elif line.startswith("- "):
            items = []
            while i < len(lines) and lines[i].startswith("- "):
                items.append({
                    "type": "listItem",
                    "content": [{"type": "paragraph", "content": _inline_nodes(lines[i][2:])}],
                })
                i += 1
            content.append({"type": "bulletList", "content": items})
            continue
        # Empty line — skip
        elif not line.strip():
            pass
        # Regular paragraph
        else:
            content.append({
                "type": "paragraph",
                "content": _inline_nodes(line),
            })
        i += 1

    if not content:
        content.append({"type": "paragraph", "content": [{"type": "text", "text": " "}]})

    return {"type": "doc", "version": 1, "content": content}


def _inline_nodes(text: str) -> list[dict]:
    """Parse inline markdown (bold, code, links) into ADF inline nodes."""
    nodes: list[dict] = []
    # Split by bold, code, and links
    pattern = re.compile(r"(\*\*(.+?)\*\*|`([^`]+)`|(https?://\S+))")
    last = 0
    for m in pattern.finditer(text):
        # Plain text before match
        if m.start() > last:
            nodes.append({"type": "text", "text": text[last:m.start()]})

        if m.group(2):  # bold
            nodes.append({
                "type": "text", "text": m.group(2),
                "marks": [{"type": "strong"}],
            })
        elif m.group(3):  # code
            nodes.append({
                "type": "text", "text": m.group(3),
                "marks": [{"type": "code"}],
            })
        elif m.group(4):  # link
            url = m.group(4)
            nodes.append({
                "type": "text", "text": url,
                "marks": [{"type": "link", "attrs": {"href": url}}],
            })
        last = m.end()

    # Remaining text
    if last < len(text):
        nodes.append({"type": "text", "text": text[last:]})

    if not nodes:
        nodes.append({"type": "text", "text": " "})

    return nodes
