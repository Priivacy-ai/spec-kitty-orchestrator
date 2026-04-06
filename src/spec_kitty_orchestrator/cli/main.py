"""CLI entrypoint for spec-kitty-orchestrator.

Usage:
    spec-kitty-orchestrator orchestrate --mission <slug> [options]
    spec-kitty-orchestrator status
    spec-kitty-orchestrator resume
    spec-kitty-orchestrator abort [--cleanup-worktrees]
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from ..config import load_config
from ..host.client import HostClient, ContractMismatchError
from ..loop import OrchestrationError, run_orchestration_loop
from ..policy import PolicyMetadata
from ..state import load_state, new_run_state, save_state

app = typer.Typer(
    name="spec-kitty-orchestrator",
    help="External orchestrator for spec-kitty workflow, driven by the orchestrator-api contract.",
    no_args_is_help=True,
)
console = Console()

_DEFAULT_ACTOR = "spec-kitty-orchestrator"
_DEFAULT_POLICY = PolicyMetadata(
    orchestrator_id="spec-kitty-orchestrator",
    orchestrator_version="0.1.0",
    agent_family="claude",
    approval_mode="full_auto",
    sandbox_mode="workspace_write",
    network_mode="none",
    dangerous_flags=[],
    tool_restrictions=None,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


def _find_repo_root() -> Path:
    """Walk up from cwd to find a .kittify directory."""
    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        if (parent / ".kittify").exists():
            return parent
    return cwd


@app.command()
def orchestrate(
    mission: str = typer.Option(..., "--mission", "-m", help="Mission slug to orchestrate"),
    impl_agent: Optional[str] = typer.Option(None, "--impl-agent", help="Override implementation agent"),
    review_agent: Optional[str] = typer.Option(None, "--review-agent", help="Override review agent"),
    max_concurrent: int = typer.Option(4, "--max-concurrent", help="Max concurrent WPs"),
    actor: str = typer.Option(_DEFAULT_ACTOR, "--actor", help="Actor identity"),
    repo_root: Optional[str] = typer.Option(None, "--repo-root", help="Override repo root path"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Validate config without running"),
) -> None:
    """Orchestrate all WPs for a mission through implementation and review."""
    root = Path(repo_root) if repo_root else _find_repo_root()

    cfg_overrides = {"max_concurrent_wps": max_concurrent}
    if impl_agent:
        cfg_overrides["implementation_agents"] = [impl_agent]
    if review_agent:
        cfg_overrides["review_agents"] = [review_agent]

    cfg = load_config(root, actor, **cfg_overrides)

    policy = _DEFAULT_POLICY
    try:
        policy.validate()
    except ValueError as exc:
        console.print(f"[red]Policy validation failed:[/red] {exc}")
        raise typer.Exit(1)

    host = HostClient(
        repo_root=root,
        actor=actor,
        policy_json=policy.to_json(),
    )

    # Validate contract version
    try:
        ver = host.contract_version()
        console.print(f"Host contract version: [cyan]{ver.api_version}[/cyan]")
    except ContractMismatchError as exc:
        console.print(f"[red]Contract version mismatch:[/red] {exc}")
        raise typer.Exit(1)
    except RuntimeError as exc:
        console.print(f"[red]Cannot connect to spec-kitty:[/red] {exc}")
        raise typer.Exit(1)

    if dry_run:
        console.print("[green]Dry run: configuration valid.[/green]")
        console.print(f"  Mission: {mission}")
        console.print(f"  Impl agents: {cfg.agent_selection.implementation_agents}")
        console.print(f"  Review agents: {cfg.agent_selection.review_agents}")
        return

    run_state = new_run_state(mission, policy)
    save_state(run_state, cfg.state_file)

    console.print(f"[bold green]Starting orchestration[/bold green] for mission [cyan]{mission}[/cyan]")
    console.print(f"  Run ID: {run_state.run_id}")
    console.print(f"  Impl agents: {cfg.agent_selection.implementation_agents}")
    console.print(f"  Max concurrent: {cfg.max_concurrent_wps}")

    try:
        asyncio.run(run_orchestration_loop(mission, host, run_state, cfg))
        console.print("[bold green]Orchestration completed successfully.[/bold green]")
    except OrchestrationError as exc:
        console.print(f"[red]Orchestration error:[/red] {exc}")
        raise typer.Exit(1)
    except KeyboardInterrupt:
        console.print("[yellow]Interrupted by user.[/yellow]")
        save_state(run_state, cfg.state_file)
        raise typer.Exit(130)


@app.command()
def status(
    repo_root: Optional[str] = typer.Option(None, "--repo-root", help="Override repo root path"),
) -> None:
    """Show the current run state from the most recent orchestration."""
    root = Path(repo_root) if repo_root else _find_repo_root()
    state_file = root / ".kittify" / "orchestrator-run-state.json"
    run_state = load_state(state_file)

    if run_state is None:
        console.print("[yellow]No orchestration run state found.[/yellow]")
        raise typer.Exit(0)

    console.print(f"[bold]Run ID:[/bold] {run_state.run_id}")
    console.print(f"[bold]Mission:[/bold] {run_state.mission_slug}")
    console.print(f"[bold]Started:[/bold] {run_state.started_at}")

    table = Table("WP", "Impl Agent", "Impl Retries", "Review Agent", "Review Retries", "Error")
    for wp_id, wp_exec in sorted(run_state.wp_executions.items()):
        table.add_row(
            wp_id,
            wp_exec.implementation_agent or "-",
            str(wp_exec.implementation_retries),
            wp_exec.review_agent or "-",
            str(wp_exec.review_retries),
            (wp_exec.last_error or "")[:60],
        )
    console.print(table)


@app.command()
def resume(
    actor: str = typer.Option(_DEFAULT_ACTOR, "--actor", help="Actor identity"),
    repo_root: Optional[str] = typer.Option(None, "--repo-root", help="Override repo root path"),
) -> None:
    """Resume an interrupted orchestration run from saved state."""
    root = Path(repo_root) if repo_root else _find_repo_root()
    state_file = root / ".kittify" / "orchestrator-run-state.json"
    run_state = load_state(state_file)

    if run_state is None:
        console.print("[red]No run state found to resume.[/red]")
        raise typer.Exit(1)

    console.print(f"Resuming run [cyan]{run_state.run_id}[/cyan] for mission [cyan]{run_state.mission_slug}[/cyan]")

    policy = run_state.policy
    cfg = load_config(root, actor)
    host = HostClient(root, actor, policy_json=policy.to_json())

    try:
        asyncio.run(run_orchestration_loop(run_state.mission_slug, host, run_state, cfg))
        console.print("[bold green]Resumed orchestration completed.[/bold green]")
    except OrchestrationError as exc:
        console.print(f"[red]Orchestration error:[/red] {exc}")
        raise typer.Exit(1)
    except KeyboardInterrupt:
        console.print("[yellow]Interrupted.[/yellow]")
        save_state(run_state, cfg.state_file)
        raise typer.Exit(130)


@app.command()
def abort(
    cleanup_worktrees: bool = typer.Option(False, "--cleanup-worktrees", help="Remove provider-local state files"),
    repo_root: Optional[str] = typer.Option(None, "--repo-root", help="Override repo root path"),
) -> None:
    """Mark the current orchestration as aborted."""
    root = Path(repo_root) if repo_root else _find_repo_root()
    state_file = root / ".kittify" / "orchestrator-run-state.json"

    if not state_file.exists():
        console.print("[yellow]No active run state found.[/yellow]")
        raise typer.Exit(0)

    if cleanup_worktrees:
        state_file.unlink(missing_ok=True)
        console.print("[green]Run state removed.[/green]")
    else:
        console.print(f"[yellow]Abort recorded. Run state preserved at {state_file}[/yellow]")
        console.print("Re-run with --cleanup-worktrees to remove state file.")


def main() -> None:
    """Entry point."""
    app()


if __name__ == "__main__":
    main()
