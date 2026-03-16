"""Migrate old config.yaml + .env to new TOML config format."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

log = logging.getLogger(__name__)

# Common locations to search for old config
_SEARCH_DIRS = [
    Path.home() / "Dev" / "Python" / "jira-symphony",
]


def find_old_config(
    search_dir: Path | None = None,
) -> tuple[Path | None, Path | None]:
    """Look for old config.yaml and .env files."""
    candidates = []
    if search_dir:
        candidates.append(search_dir)
    candidates.extend(_SEARCH_DIRS)
    candidates.append(Path.cwd())

    config_yaml = None
    env_file = None

    for d in candidates:
        if not d.exists():
            continue
        c = d / "config.yaml"
        e = d / ".env"
        if c.exists() and config_yaml is None:
            config_yaml = c
        if e.exists() and env_file is None:
            env_file = e
        if config_yaml and env_file:
            break

    return config_yaml, env_file


def _load_env_file(env_path: Path) -> dict[str, str]:
    """Parse a .env file into a dict."""
    env_vars: dict[str, str] = {}
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, value = line.split("=", 1)
            env_vars[key.strip()] = value.strip()
    return env_vars


def _interpolate(value: str, env_vars: dict[str, str]) -> str:
    """Replace ${VAR} with values from env_vars or os.environ."""
    def _replace(match: re.Match) -> str:
        var = match.group(1)
        return env_vars.get(var, os.environ.get(var, ""))
    return re.sub(r"\$\{(\w+)\}", _replace, value)


def migrate_config(
    config_yaml: Path,
    env_file: Path | None = None,
    output_path: Path | None = None,
) -> "SymphonyConfig":
    """Convert old config.yaml + .env to new SymphonyConfig.

    Requires pyyaml (install with: pip install jira-symphony[migration]).
    """
    try:
        import yaml
    except ImportError:
        raise ImportError(
            "pyyaml is required for migration. "
            "Install with: pip install jira-symphony[migration]"
        )

    from .config import (
        ClaudeConfig,
        GitLabConfig,
        JiraConfig,
        JiraFiltersConfig,
        JiraTransitionsConfig,
        ProjectEntry,
        RoutingConfig,
        SymphonyConfig,
        save_config,
    )

    # Load env vars from .env file
    env_vars: dict[str, str] = {}
    if env_file and env_file.exists():
        env_vars = _load_env_file(env_file)

    with open(config_yaml) as f:
        raw = yaml.safe_load(f)

    # ── Jira ──────────────────────────────────────────────
    jira_raw = raw.get("jira", {})
    auth = jira_raw.get("auth", {})
    email = _interpolate(auth.get("email", ""), env_vars)
    api_token = _interpolate(auth.get("api_token", ""), env_vars)

    # Filters: old had singular IDs, new has lists
    filters_raw = jira_raw.get("filters", {})
    reporter_ids = []
    if "reporter_account_id" in filters_raw:
        reporter_ids = [filters_raw["reporter_account_id"]]
    assignee_ids = []
    if "assignee_account_id" in filters_raw:
        assignee_ids = [filters_raw["assignee_account_id"]]

    # Transitions
    transitions_raw = jira_raw.get("transitions", {})

    jira_config = JiraConfig(
        cloud_id=jira_raw.get("cloud_id", ""),
        site=jira_raw.get("site", ""),
        project_key=jira_raw.get("project_key", ""),
        poll_interval_seconds=jira_raw.get("poll_interval_seconds", 30),
        email=email,
        api_token=api_token,
        filters=JiraFiltersConfig(
            statuses=["К выполнению", "Front", "тест не пройден"],
            reporter_account_ids=reporter_ids,
            assignee_account_ids=assignee_ids,
        ),
        transitions=JiraTransitionsConfig(**transitions_raw),
    )

    # ── GitLab ────────────────────────────────────────────
    gitlab_raw = raw.get("gitlab", {})
    gitlab_token = _interpolate(gitlab_raw.get("api_token", ""), env_vars)
    gitlab_config = GitLabConfig(
        token=gitlab_token,
        base_url=gitlab_raw.get("base_url", "https://gitlab.com"),
    )

    # ── Claude ────────────────────────────────────────────
    claude_raw = raw.get("claude", {})
    retry_raw = claude_raw.get("retry", {})
    claude_config = ClaudeConfig(
        max_workers=claude_raw.get("max_workers", 3),
        model=claude_raw.get("model", "opus"),
        max_budget_usd=claude_raw.get("max_budget_usd", 5.0),
        timeout_minutes=claude_raw.get("timeout_minutes", 30),
        max_retry_attempts=retry_raw.get("max_attempts", 2),
        backoff_base_seconds=retry_raw.get("backoff_base_seconds", 60),
    )

    # ── Routing ───────────────────────────────────────────
    routing_raw = raw.get("routing", {})
    routing_config = RoutingConfig(
        triage_enabled=routing_raw.get("triage_enabled", True),
        triage_model=routing_raw.get("triage_model", "sonnet"),
        epic_map=routing_raw.get("epic_map", {}),
        label_map=routing_raw.get("label_map", {}),
    )

    # ── Projects: dict-of-dicts -> list ───────────────────
    projects_raw = raw.get("projects", {})
    projects = []
    for name, proj in projects_raw.items():
        projects.append(ProjectEntry(
            name=name,
            path=proj.get("path", ""),
            description="",
            main_branch=proj.get("main_branch", "main"),
            git_provider="gitlab",
            git_remote=proj.get("gitlab_project", ""),
            pr_target_branch=proj.get("mr_target_branch", "main"),
            transition_id=proj.get("transition_id", "3"),
            extra_dirs=proj.get("extra_dirs", []),
        ))

    config = SymphonyConfig(
        jira=jira_config,
        claude=claude_config,
        routing=routing_config,
        gitlab=gitlab_config,
        projects=projects,
    )

    # Save to new location
    saved_path = save_config(config, output_path)
    log.info("Migration complete: %s", saved_path)

    return config
