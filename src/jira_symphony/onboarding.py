"""Interactive onboarding wizard for jira-symphony init."""

from __future__ import annotations

import logging
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt

from .config import (
    CONFIG_PATH,
    ClaudeConfig,
    GitHubConfig,
    GitLabConfig,
    JiraConfig,
    JiraFiltersConfig,
    JiraTransitionsConfig,
    ProjectEntry,
    RoutingConfig,
    SymphonyConfig,
    save_config,
)
from .migration import find_old_config, migrate_config

log = logging.getLogger(__name__)

console = Console()


def run_wizard() -> SymphonyConfig:
    """Run the interactive onboarding wizard."""
    console.print(Panel(
        "[bold]Jira Symphony[/bold] \u2014 Setup Wizard",
        subtitle="v0.2",
        border_style="blue",
    ))

    # Step 1: Migration check
    config = _check_migration()
    if config:
        return config

    # Step 2: Jira connection
    console.print("\n[bold blue]Step 1:[/bold blue] Jira Connection")
    jira_site = Prompt.ask("Jira site (e.g. myorg.atlassian.net)")
    jira_email = Prompt.ask("Jira email")
    jira_api_token = Prompt.ask("Jira API token", password=True)
    jira_cloud_id = Prompt.ask("Jira Cloud ID")

    # Step 3: JQL Filters
    console.print("\n[bold blue]Step 2:[/bold blue] JQL Filters")
    project_key = Prompt.ask("Jira project key (e.g. PROJ)")
    poll_interval = int(Prompt.ask("Poll interval (seconds)", default="30"))

    statuses_str = Prompt.ask(
        "Statuses to poll (comma-separated)",
        default="\u041a \u0432\u044b\u043f\u043e\u043b\u043d\u0435\u043d\u0438\u044e, Front, \u0442\u0435\u0441\u0442 \u043d\u0435 \u043f\u0440\u043e\u0439\u0434\u0435\u043d",
    )
    statuses = [s.strip() for s in statuses_str.split(",")]

    reporter_ids_str = Prompt.ask(
        "Reporter account IDs (comma-separated, or empty)", default=""
    )
    reporter_ids = [s.strip() for s in reporter_ids_str.split(",") if s.strip()]

    assignee_ids_str = Prompt.ask(
        "Assignee account IDs (comma-separated, or empty)", default=""
    )
    assignee_ids = [s.strip() for s in assignee_ids_str.split(",") if s.strip()]

    # Step 4: Transitions
    console.print("\n[bold blue]Step 3:[/bold blue] Jira Transitions")
    in_progress = Prompt.ask("In-progress transition ID", default="3")
    qa = Prompt.ask("QA transition ID", default="6")
    done = Prompt.ask("Done transition ID", default="13")
    to_do = Prompt.ask("To-Do transition ID", default="11")

    # Step 5: Git providers
    console.print("\n[bold blue]Step 4:[/bold blue] Git Providers")

    github_config = GitHubConfig()
    gitlab_config = GitLabConfig()

    use_gitlab = Confirm.ask("Configure GitLab?", default=True)
    if use_gitlab:
        gitlab_token = Prompt.ask("GitLab token", password=True)
        gitlab_url = Prompt.ask("GitLab base URL", default="https://gitlab.com")
        gitlab_config = GitLabConfig(token=gitlab_token, base_url=gitlab_url)

    use_github = Confirm.ask("Configure GitHub?", default=False)
    if use_github:
        github_token = Prompt.ask("GitHub token", password=True)
        github_config = GitHubConfig(token=github_token)

    # Step 6: Claude settings
    console.print("\n[bold blue]Step 5:[/bold blue] Claude Settings")
    max_workers = int(Prompt.ask("Max concurrent workers", default="3"))
    model = Prompt.ask("Model", default="opus")
    budget = float(Prompt.ask("Max budget per task (USD)", default="5.0"))
    timeout = int(Prompt.ask("Timeout per task (minutes)", default="30"))
    retries = int(Prompt.ask("Max retry attempts", default="2"))

    # Step 7: Projects
    console.print("\n[bold blue]Step 6:[/bold blue] Projects")
    projects: list[ProjectEntry] = []
    while True:
        if projects:
            add_more = Confirm.ask("Add another project?", default=False)
            if not add_more:
                break
        else:
            console.print("Let's add your first project.")

        proj = _prompt_project(use_gitlab, use_github)
        projects.append(proj)

    config = SymphonyConfig(
        jira=JiraConfig(
            cloud_id=jira_cloud_id,
            site=jira_site,
            project_key=project_key,
            poll_interval_seconds=poll_interval,
            email=jira_email,
            api_token=jira_api_token,
            filters=JiraFiltersConfig(
                statuses=statuses,
                reporter_account_ids=reporter_ids,
                assignee_account_ids=assignee_ids,
            ),
            transitions=JiraTransitionsConfig(
                in_progress=in_progress,
                qa=qa,
                done=done,
                to_do=to_do,
            ),
        ),
        claude=ClaudeConfig(
            max_workers=max_workers,
            model=model,
            max_budget_usd=budget,
            timeout_minutes=timeout,
            max_retry_attempts=retries,
        ),
        routing=RoutingConfig(),
        github=github_config,
        gitlab=gitlab_config,
        projects=projects,
    )

    # Step 8: Save
    path = save_config(config)
    console.print(f"\n[green]Config saved to {path}[/green]")
    return config


def _check_migration() -> SymphonyConfig | None:
    """Check for old config and offer migration."""
    config_yaml, env_file = find_old_config()
    if not config_yaml:
        return None

    console.print(f"\n[yellow]Found old config:[/yellow] {config_yaml}")
    if env_file:
        console.print(f"[yellow]Found .env:[/yellow] {env_file}")

    if Confirm.ask("Migrate existing config to new TOML format?", default=True):
        config = migrate_config(config_yaml, env_file)
        console.print(f"\n[green]Migration complete! Config saved to {CONFIG_PATH}[/green]")
        return config

    return None


def _prompt_project(has_gitlab: bool, has_github: bool) -> ProjectEntry:
    """Prompt for a single project configuration."""
    name = Prompt.ask("  Project name (e.g. admin-panel)")
    path = Prompt.ask("  Local path", default=str(Path.cwd()))
    description = Prompt.ask("  Description (optional)", default="")
    main_branch = Prompt.ask("  Main branch", default="main")

    if has_gitlab and has_github:
        provider = Prompt.ask(
            "  Git provider", choices=["gitlab", "github"], default="gitlab"
        )
    elif has_github:
        provider = "github"
    else:
        provider = "gitlab"

    git_remote = Prompt.ask("  Git remote (e.g. owner/repo)")
    pr_target = Prompt.ask("  PR/MR target branch", default=main_branch)
    transition_id = Prompt.ask("  Jira transition ID (to In Progress)", default="3")

    extra_str = Prompt.ask("  Extra dirs (comma-separated, or empty)", default="")
    extra_dirs = [s.strip() for s in extra_str.split(",") if s.strip()]

    return ProjectEntry(
        name=name,
        path=path,
        description=description,
        main_branch=main_branch,
        git_provider=provider,
        git_remote=git_remote,
        pr_target_branch=pr_target,
        transition_id=transition_id,
        extra_dirs=extra_dirs,
    )


def prompt_add_project(config: SymphonyConfig) -> ProjectEntry:
    """Interactively add a project to existing config."""
    has_gitlab = bool(config.gitlab.token)
    has_github = bool(config.github.token)
    return _prompt_project(has_gitlab, has_github)
