"""TOML-based configuration with XDG paths and Pydantic validation."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

import tomli_w
from pydantic import BaseModel, Field

from .models import ProjectConfig

# XDG-compliant config directory
CONFIG_DIR = Path(
    os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
) / "jira-symphony"
CONFIG_PATH = CONFIG_DIR / "config.toml"

# XDG-compliant data directory (for state DB)
DATA_DIR = Path(
    os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")
) / "jira-symphony"


class JiraFiltersConfig(BaseModel):
    statuses: list[str] = Field(default_factory=lambda: ["К выполнению", "Front"])
    reporter_account_ids: list[str] = Field(default_factory=list)
    assignee_account_ids: list[str] = Field(default_factory=list)


class JiraTransitionsConfig(BaseModel):
    in_progress: str = "3"
    qa: str = "6"
    done: str = "13"
    to_do: str = "11"
    model_config = {"extra": "allow"}


class JiraConfig(BaseModel):
    cloud_id: str
    site: str
    project_key: str
    poll_interval_seconds: int = 30
    email: str
    api_token: str
    filters: JiraFiltersConfig = Field(default_factory=JiraFiltersConfig)
    transitions: JiraTransitionsConfig = Field(default_factory=JiraTransitionsConfig)


class ClaudeConfig(BaseModel):
    max_workers: int = 3
    model: str = "opus"
    max_budget_usd: float = 5.0
    timeout_minutes: int = 30
    max_retry_attempts: int = 2
    backoff_base_seconds: int = 60


class RoutingConfig(BaseModel):
    triage_enabled: bool = True
    triage_model: str = "sonnet"
    epic_map: dict[str, str] = Field(default_factory=dict)
    label_map: dict[str, str] = Field(default_factory=dict)


class GitHubConfig(BaseModel):
    token: str = ""


class GitLabConfig(BaseModel):
    token: str = ""
    base_url: str = "https://gitlab.com"


class ProjectEntry(BaseModel):
    name: str
    path: str
    description: str = ""
    main_branch: str = "main"
    git_provider: str = "gitlab"
    git_remote: str = ""
    pr_target_branch: str = "main"
    transition_id: str = "3"
    extra_dirs: list[str] = Field(default_factory=list)


class SymphonyConfig(BaseModel):
    jira: JiraConfig
    claude: ClaudeConfig = Field(default_factory=ClaudeConfig)
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    github: GitHubConfig = Field(default_factory=GitHubConfig)
    gitlab: GitLabConfig = Field(default_factory=GitLabConfig)
    projects: list[ProjectEntry] = Field(default_factory=list)

    def get_project(self, key: str) -> ProjectConfig | None:
        for p in self.projects:
            if p.name == key:
                return ProjectConfig(
                    key=p.name,
                    path=p.path,
                    description=p.description,
                    main_branch=p.main_branch,
                    git_provider=p.git_provider,
                    git_remote=p.git_remote,
                    pr_target_branch=p.pr_target_branch,
                    transition_id=p.transition_id,
                    extra_dirs=p.extra_dirs,
                )
        return None

    @property
    def project_keys(self) -> set[str]:
        return {p.name for p in self.projects}


def load_config(path: Path | None = None) -> SymphonyConfig:
    """Load and validate config from TOML."""
    config_path = path or CONFIG_PATH
    if not config_path.exists():
        raise FileNotFoundError(
            f"Config not found: {config_path}\n"
            f"Run 'jira-symphony init' to create one."
        )
    with open(config_path, "rb") as f:
        raw = tomllib.load(f)
    return SymphonyConfig.model_validate(raw)


def save_config(config: SymphonyConfig, path: Path | None = None) -> Path:
    """Save config to TOML file."""
    config_path = path or CONFIG_PATH
    config_path.parent.mkdir(parents=True, exist_ok=True)

    data = config.model_dump(mode="python")
    with open(config_path, "wb") as f:
        tomli_w.dump(data, f)

    # Secure permissions on the config dir (only for default location)
    if config_path.parent == CONFIG_DIR:
        try:
            os.chmod(config_path.parent, 0o700)
        except OSError:
            pass
    return config_path


def config_exists(path: Path | None = None) -> bool:
    """Check if config file exists."""
    return (path or CONFIG_PATH).exists()
