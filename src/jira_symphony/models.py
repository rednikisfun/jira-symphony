"""Data models for Jira Symphony."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime


class WorkerStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


@dataclass
class JiraIssue:
    key: str
    summary: str
    description: str | None
    issue_type: str
    priority: str
    labels: list[str]
    parent_key: str | None  # epic parent
    reporter_id: str
    assignee_id: str

    @classmethod
    def from_api(cls, data: dict) -> JiraIssue:
        fields = data["fields"]
        parent = fields.get("parent")
        return cls(
            key=data["key"],
            summary=fields.get("summary", ""),
            description=_extract_text(fields.get("description")),
            issue_type=fields.get("issuetype", {}).get("name", ""),
            priority=fields.get("priority", {}).get("name", "Medium"),
            labels=fields.get("labels", []),
            parent_key=parent["key"] if parent else None,
            reporter_id=fields.get("reporter", {}).get("accountId", ""),
            assignee_id=fields.get("assignee", {}).get("accountId", ""),
        )


@dataclass
class Worker:
    issue_key: str
    project_key: str
    worktree_path: str
    branch_name: str
    status: WorkerStatus = WorkerStatus.PENDING
    pid: int | None = None
    session_id: str | None = None
    attempt: int = 1
    started_at: datetime | None = None
    finished_at: datetime | None = None
    output: str = ""
    error: str = ""
    pr_url: str = ""


@dataclass
class ProjectConfig:
    key: str
    path: str
    main_branch: str
    git_provider: str
    git_remote: str
    pr_target_branch: str
    transition_id: str
    description: str = ""
    extra_dirs: list[str] = field(default_factory=list)


def _extract_text(desc: dict | str | None) -> str | None:
    """Extract plain text from Atlassian Document Format or plain string."""
    if desc is None:
        return None
    if isinstance(desc, str):
        return desc
    # ADF (Atlassian Document Format) — walk content nodes
    if isinstance(desc, dict) and desc.get("type") == "doc":
        return _walk_adf(desc)
    return str(desc)


def _walk_adf(node: dict) -> str:
    """Recursively extract text from ADF nodes."""
    parts: list[str] = []
    if node.get("type") == "text":
        parts.append(node.get("text", ""))
    for child in node.get("content", []):
        parts.append(_walk_adf(child))
    # Add newlines after block-level nodes
    if node.get("type") in ("paragraph", "heading", "bulletList", "orderedList", "listItem"):
        parts.append("\n")
    return "".join(parts)
