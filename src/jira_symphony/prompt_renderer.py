"""Jinja2-based prompt rendering for Claude Code sessions."""

from __future__ import annotations

from importlib import resources
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from .models import JiraIssue, ProjectConfig

# Resolve template dir from within the installed package
_PKG_TEMPLATE_DIR = Path(str(resources.files("jira_symphony").joinpath("prompt_templates")))


class PromptRenderer:
    """Renders prompt templates with issue context."""

    def __init__(self, template_dir: Path | None = None) -> None:
        tdir = template_dir or _PKG_TEMPLATE_DIR
        self._env = Environment(
            loader=FileSystemLoader(str(tdir)),
            keep_trailing_newline=True,
        )

    def render(
        self,
        issue: JiraIssue,
        project: ProjectConfig | None = None,
        template_name: str = "default.md.j2",
    ) -> str:
        template = self._env.get_template(template_name)
        return template.render(issue=issue, project=project)
