"""3-layer issue-to-project routing: epic -> labels -> LLM triage."""

from __future__ import annotations

import asyncio
import json
import logging

from .config import SymphonyConfig
from .models import JiraIssue

log = logging.getLogger(__name__)


class Router:
    """Routes Jira issues to target projects using 3-layer strategy."""

    def __init__(self, config: SymphonyConfig) -> None:
        self._epic_map = config.routing.epic_map
        self._label_map = config.routing.label_map
        self._triage_enabled = config.routing.triage_enabled
        self._triage_model = config.routing.triage_model
        self._project_keys = config.project_keys
        # Build project descriptions from config
        self._project_descriptions = {
            p.name: p.description or p.name
            for p in config.projects
        }

    def route(self, issue: JiraIssue) -> str | None:
        """Synchronous routing via epic and label layers. Returns project key or None."""
        # Layer 1: Epic parent
        if issue.parent_key and issue.parent_key in self._epic_map:
            project = self._epic_map[issue.parent_key]
            log.info("%s -> %s (via epic %s)", issue.key, project, issue.parent_key)
            return project

        # Layer 2: Labels
        for label in issue.labels:
            if label in self._label_map:
                project = self._label_map[label]
                log.info("%s -> %s (via label %s)", issue.key, project, label)
                return project

        return None

    async def route_with_triage(self, issue: JiraIssue) -> str | None:
        """Full routing with LLM triage fallback."""
        result = self.route(issue)
        if result:
            return result

        if not self._triage_enabled:
            log.warning("%s: no route found, triage disabled", issue.key)
            return None

        # Layer 3: LLM triage
        return await self._llm_triage(issue)

    async def _llm_triage(self, issue: JiraIssue) -> str | None:
        """Use Claude CLI (subscription) to classify the issue into a project."""
        log.info("%s: falling back to LLM triage", issue.key)

        project_list = "\n".join(
            f"- {key}: {desc}" for key, desc in self._project_descriptions.items()
        )
        prompt = (
            f"Classify this Jira issue into one of the following projects.\n\n"
            f"## Projects\n{project_list}\n\n"
            f"## Issue\n"
            f"**Key:** {issue.key}\n"
            f"**Type:** {issue.issue_type}\n"
            f"**Summary:** {issue.summary}\n"
            f"**Description:** {issue.description or 'N/A'}\n"
            f"**Labels:** {', '.join(issue.labels) or 'none'}\n\n"
            f'Respond with ONLY a JSON object: {{"project": "<project-key>"}}\n'
            f"Valid project keys: {', '.join(sorted(self._project_keys))}"
        )

        try:
            # Safe subprocess — uses create_subprocess_exec (no shell)
            proc = await asyncio.create_subprocess_exec(
                "claude", "-p",
                "--output-format", "json",
                "--model", self._triage_model,
                "--max-turns", "1",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=prompt.encode()), timeout=60
            )

            if proc.returncode != 0:
                log.error("%s: claude CLI failed: %s", issue.key, stderr.decode()[:200])
                return None

            outer = json.loads(stdout.decode())
            text = outer.get("result", stdout.decode()).strip()

            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                start = text.find("{")
                end = text.rfind("}") + 1
                if start >= 0 and end > start:
                    parsed = json.loads(text[start:end])
                else:
                    log.warning("%s: LLM returned non-JSON: %s", issue.key, text[:100])
                    return None

            project = parsed.get("project")

            if project in self._project_keys:
                log.info("%s -> %s (via LLM triage)", issue.key, project)
                return project
            else:
                log.warning("%s: LLM returned unknown project %r", issue.key, project)
                return None

        except asyncio.TimeoutError:
            log.error("%s: LLM triage timed out", issue.key)
            return None
        except Exception:
            log.exception("%s: LLM triage failed", issue.key)
            return None
