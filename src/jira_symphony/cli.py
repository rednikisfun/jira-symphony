"""Typer CLI application for Jira Symphony."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .config import CONFIG_PATH, config_exists, load_config, save_config

app = typer.Typer(
    name="jira-symphony",
    help="Autonomous Jira-to-Claude Code orchestrator",
    no_args_is_help=True,
)

projects_app = typer.Typer(help="Manage configured projects")
config_app = typer.Typer(help="Configuration utilities")
app.add_typer(projects_app, name="projects")
app.add_typer(config_app, name="config")

console = Console()
DEFAULT_PORT = 8787


def _setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


@app.command()
def init():
    """Interactive onboarding wizard — set up Jira Symphony."""
    from .onboarding import run_wizard

    if config_exists():
        console.print(f"[yellow]Config already exists at {CONFIG_PATH}[/yellow]")
        if not typer.confirm("Overwrite?"):
            raise typer.Exit()

    run_wizard()


@app.command()
def start(
    port: int = typer.Option(DEFAULT_PORT, "--port", "-p", help="Web dashboard port"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose logging"),
):
    """Run the orchestrator and web dashboard."""
    _setup_logging(verbose)
    log = logging.getLogger("jira_symphony")

    if not config_exists():
        console.print("[red]No config found. Run 'jira-symphony init' first.[/red]")
        raise typer.Exit(1)

    log.info("Starting Jira Symphony (dashboard on port %d)", port)

    async def _run():
        import signal

        import uvicorn

        from .orchestrator import Orchestrator
        from .web import app as web_app, set_orchestrator

        config = load_config()
        orch = Orchestrator(config)
        set_orchestrator(orch)

        loop = asyncio.get_running_loop()

        def _shutdown():
            log.info("Received shutdown signal")
            asyncio.ensure_future(orch.stop())

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _shutdown)

        uvi_config = uvicorn.Config(
            web_app, host="0.0.0.0", port=port,
            log_level="warning", access_log=False,
        )
        server = uvicorn.Server(uvi_config)
        log.info("Dashboard: http://localhost:%d", port)

        await asyncio.gather(orch.start(), server.serve())

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


@app.command()
def status(
    port: int = typer.Option(DEFAULT_PORT, "--port", "-p", help="Running instance port"),
):
    """Show status of the running orchestrator."""
    import httpx

    try:
        resp = httpx.get(f"http://localhost:{port}/api/status", timeout=5)
        resp.raise_for_status()
        data = resp.json()
    except httpx.ConnectError:
        console.print(f"[red]Cannot connect to localhost:{port}. Is jira-symphony running?[/red]")
        raise typer.Exit(1)

    table = Table(title="Jira Symphony Status")
    table.add_column("Property", style="bold")
    table.add_column("Value")

    table.add_row("Running", "[green]Yes[/green]" if data["running"] else "[red]No[/red]")
    table.add_row("Paused", "[yellow]Yes[/yellow]" if data["paused"] else "No")
    table.add_row("Active Workers", f'{data["active_workers"]}/{data["max_workers"]}')
    table.add_row("Dispatched", str(data["total_dispatched"]))
    table.add_row("Completed", f'[green]{data["total_completed"]}[/green]')
    table.add_row("Failed", f'[red]{data["total_failed"]}[/red]')
    table.add_row("Last Poll", data.get("last_poll_at") or "\u2014")

    console.print(table)

    if data.get("workers"):
        console.print("\n[bold]Active Workers:[/bold]")
        for w in data["workers"]:
            p = w.get("progress", {})
            line = (
                f"  {w['issue']} -> {w['project']} "
                f"[{w['status']}] "
                f"{w.get('elapsed', '')} "
                f"| {p.get('current_activity', '')}"
            )
            console.print(line)
            if p.get("remote_url"):
                console.print(f"    [link={p['remote_url']}]{p['remote_url']}[/link]")


@app.command()
def add(
    issue_key: str = typer.Argument(help="Jira issue key (e.g. STCH-1234)"),
    project: Optional[str] = typer.Option(None, "--project", help="Target project name"),
    port: int = typer.Option(DEFAULT_PORT, "--port", "-p", help="Running instance port"),
):
    """Manually dispatch a Jira issue."""
    import httpx

    try:
        body: dict = {"issue_key": issue_key}
        if project:
            body["project"] = project
        resp = httpx.post(
            f"http://localhost:{port}/api/dispatch", json=body, timeout=10
        )
        resp.raise_for_status()
        data = resp.json()
        console.print(f"[green]{data.get('message', 'Dispatched')}[/green]")
    except httpx.ConnectError:
        console.print(f"[red]Cannot connect to localhost:{port}. Is jira-symphony running?[/red]")
        raise typer.Exit(1)
    except httpx.HTTPStatusError as e:
        console.print(f"[red]Error: {e.response.status_code} — {e.response.text}[/red]")
        raise typer.Exit(1)


# ── Projects sub-commands ─────────────────────────────────


@projects_app.command("list")
def projects_list():
    """Show all configured projects."""
    if not config_exists():
        console.print("[red]No config found. Run 'jira-symphony init' first.[/red]")
        raise typer.Exit(1)

    config = load_config()
    table = Table(title="Configured Projects")
    table.add_column("Name", style="bold")
    table.add_column("Path")
    table.add_column("Provider")
    table.add_column("Remote")
    table.add_column("Branch")
    table.add_column("PR Target")

    for p in config.projects:
        table.add_row(
            p.name, p.path, p.git_provider,
            p.git_remote, p.main_branch, p.pr_target_branch,
        )

    console.print(table)


@projects_app.command("add")
def projects_add():
    """Interactively add a project to the config."""
    if not config_exists():
        console.print("[red]No config found. Run 'jira-symphony init' first.[/red]")
        raise typer.Exit(1)

    from .onboarding import prompt_add_project

    config = load_config()
    proj = prompt_add_project(config)
    config.projects.append(proj)
    save_config(config)
    console.print(f"[green]Project '{proj.name}' added.[/green]")


@projects_app.command("remove")
def projects_remove(
    name: str = typer.Argument(help="Project name to remove"),
):
    """Remove a project from the config."""
    if not config_exists():
        console.print("[red]No config found. Run 'jira-symphony init' first.[/red]")
        raise typer.Exit(1)

    config = load_config()
    original_count = len(config.projects)
    config.projects = [p for p in config.projects if p.name != name]

    if len(config.projects) == original_count:
        console.print(f"[yellow]Project '{name}' not found.[/yellow]")
        raise typer.Exit(1)

    save_config(config)
    console.print(f"[green]Project '{name}' removed.[/green]")


# ── Config sub-commands ───────────────────────────────────


@config_app.command("path")
def config_path():
    """Print the config file path."""
    console.print(str(CONFIG_PATH))


@config_app.command("edit")
def config_edit():
    """Open config in $EDITOR."""
    if not config_exists():
        console.print("[red]No config found. Run 'jira-symphony init' first.[/red]")
        raise typer.Exit(1)

    editor = os.environ.get("EDITOR", "vim")
    subprocess.run([editor, str(CONFIG_PATH)])


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version", "-V", help="Show version"),
):
    """Jira Symphony — Autonomous Jira-to-Claude Code orchestrator."""
    if version:
        console.print(f"jira-symphony {__version__}")
        raise typer.Exit()
