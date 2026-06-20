"""Regression tests for how the CLI wires host-mutation repo roots.

How the bug bit (the scenario these tests guard against)
--------------------------------------------------------
1. A user runs ``spec-kitty-orchestrator orchestrate --mission <slug>``.
2. ``orchestrate`` called ``prepare_mission_worktree(root, mission)``, which ran
   ``git worktree add`` to create ``.worktrees/<slug>-orchestrator`` and returned
   that path.
3. That worktree path was handed to ``HostClient`` as ``history_repo_root``, so
   ``append_history`` invoked ``spec-kitty orchestrator-api append-history`` with
   its cwd/SPECIFY_REPO_ROOT pointed *inside* the worktree.
4. append-history commits a planning artifact (a WP prompt file). spec-kitty's
   SAFE_COMMIT_PATH_POLICY deliberately refuses to commit planning artifacts
   from inside a worktree, so the call was rejected and orchestration died part
   way through the loop.

The orchestrator worktree was never actually used for anything else: agents run
in the WP worktrees the host API returns (created off the primary checkout by
``executor.ensure_working_dir``), and every other ``HostClient`` command already
ran from the primary checkout. So the fix is to stop creating that worktree and
run *all* host mutations, append-history included, from the primary checkout.

Why --dry-run is a faithful proxy
---------------------------------
The append-history rejection only reproduces against a live spec-kitty with a
real mission. But the root cause is observable far earlier and far more cheaply:
``prepare_mission_worktree`` ran unconditionally, *before* the ``--dry-run``
short-circuit, so the stray ``.worktrees/<slug>-orchestrator`` was created even
on a dry run. Asserting that no orchestrator worktree is created therefore pins
the exact decision that caused the policy rejection, without needing spec-kitty
installed or a mission on disk.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from spec_kitty_orchestrator.cli.main import app
from spec_kitty_orchestrator.host.models import ContractVersionData


def _init_repo(root: Path) -> None:
    """Create a minimal committed git repo with a .kittify dir."""
    (root / ".kittify").mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=root, check=True)


def test_orchestrate_does_not_create_orchestrator_worktree(tmp_path: Path) -> None:
    """orchestrate must not create a ``.worktrees/<mission>-orchestrator`` worktree.

    See the module docstring for the full scenario. In short: creating that
    worktree is the step that later routed append-history through a worktree and
    tripped spec-kitty's SAFE_COMMIT_PATH_POLICY. ``--dry-run`` exercises the
    worktree-creation decision without needing a live spec-kitty.
    """
    _init_repo(tmp_path)

    fake_version = ContractVersionData(
        api_version="1.0.0", min_supported_provider_version="0.1.0"
    )
    with patch(
        "spec_kitty_orchestrator.cli.main.HostClient.contract_version",
        return_value=fake_version,
    ):
        result = CliRunner().invoke(
            app,
            [
                "orchestrate",
                "--mission", "099-test",
                "--repo-root", str(tmp_path),
                "--dry-run",
            ],
        )

    assert result.exit_code == 0, result.output
    # The old code created `.worktrees/099-test-orchestrator` here (even on a dry
    # run) and pointed history commits at it -> SAFE_COMMIT_PATH_POLICY rejection.
    assert not (tmp_path / ".worktrees").exists(), (
        "orchestrate created an orchestrator worktree; host mutations must run "
        "from the primary checkout, never from a worktree"
    )
