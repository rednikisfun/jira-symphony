# Jira Symphony

Autonomous Jira-to-Claude Code orchestrator. Polls Jira for issues, routes them to the right project, spawns Claude Code workers in isolated git worktrees, and creates PRs/MRs when done.

## Quick Start

```bash
pip install -e .
jira-symphony init      # Interactive setup wizard
jira-symphony start     # Run orchestrator + dashboard
```

## How It Works

1. **Poll** — Fetches To Do issues from Jira matching your JQL filters
2. **Route** — Maps issues to projects via epic, label, or LLM triage
3. **Dispatch** — Creates a git worktree, spawns Claude Code with a prompt
4. **Complete** — Pushes the branch, creates a PR/MR, comments on Jira, transitions to QA

## CLI Commands

```
jira-symphony init                              # Interactive onboarding wizard
jira-symphony start [--port 8787] [-v]          # Run orchestrator + web dashboard
jira-symphony status                            # Print running instance status
jira-symphony add ISSUE-KEY [--project name]    # Manual dispatch
jira-symphony projects list                     # Show configured projects
jira-symphony projects add                      # Add a project interactively
jira-symphony projects remove NAME              # Remove a project
jira-symphony config path                       # Print config file path
jira-symphony config edit                       # Open config in $EDITOR
```

## Configuration

Config lives at `~/.config/jira-symphony/config.toml`. Created by `jira-symphony init`.

```toml
[jira]
cloud_id = "..."
site = "myorg.atlassian.net"
project_key = "PROJ"
email = "you@example.com"
api_token = "ATATT3x..."

[jira.filters]
statuses = ["To Do", "Ready"]
reporter_account_ids = ["712020:abc..."]

[jira.transitions]
in_progress = "3"
qa = "6"

[claude]
max_workers = 3
model = "opus"
max_budget_usd = 5.0

[gitlab]
token = "glpat-..."

[github]
token = "ghp_..."

[[projects]]
name = "my-app"
path = "/path/to/repo"
git_provider = "gitlab"
git_remote = "org/repo"
pr_target_branch = "main"
```

## Git Providers

Supports both **GitLab** (merge requests) and **GitHub** (pull requests). Set `git_provider` per project.

## Migration

If you have an existing `config.yaml` + `.env` setup, `jira-symphony init` will detect and offer to migrate automatically.

## Web Dashboard

Visit `http://localhost:8787` when running. Features:
- Real-time worker status via SSE
- Manual issue dispatch
- Kill/retry workers
- Stream log viewer
- Setup wizard at `/setup`

## License

MIT
