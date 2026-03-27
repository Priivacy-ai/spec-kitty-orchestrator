from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from spec_kitty_orchestrator.loop import (
    OrchestrationError,
    _finalize_successful_implementation,
    _ensure_workspace_exists,
    _refresh_workspace,
    _sanitize_prompt_paths,
    _workspace_requires_rebootstrap,
)


def test_sanitize_prompt_paths_rewrites_repo_root_and_adds_guardrail(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    workspace = tmp_path / "workspace"
    repo_root.mkdir()
    workspace.mkdir()

    prompt = f"Edit files under {repo_root.resolve()}/src and nowhere else."
    rewritten = _sanitize_prompt_paths(prompt, repo_root, workspace)

    assert str(repo_root.resolve()) not in rewritten
    assert str(workspace.resolve()) in rewritten
    assert "Do not edit files outside the current worktree." in rewritten


def test_workspace_requires_rebootstrap_when_clean_workspace_head_drifts(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with (
        patch("spec_kitty_orchestrator.loop._git_status_lines", return_value=[]),
        patch("spec_kitty_orchestrator.loop._workspace_metadata_base_commit", return_value="base-commit"),
        patch("spec_kitty_orchestrator.loop._git_head", return_value="other-commit"),
    ):
        assert _workspace_requires_rebootstrap(tmp_path, "010-test-feature", "WP01", workspace) is True


def test_finalize_successful_implementation_rejects_empty_success(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    host = Mock()

    with (
        patch("spec_kitty_orchestrator.loop._git_status_lines", return_value=[]),
        patch("spec_kitty_orchestrator.loop._git_head", return_value="same-head"),
    ):
        with pytest.raises(OrchestrationError, match="without any worktree changes or commits"):
            _finalize_successful_implementation(
                host=host,
                workspace_path=workspace,
                feature="010-test-feature",
                wp_id="WP01",
                prompt_text="---\nsubtasks:\n- T001\n---\n",
                baseline_head="same-head",
                context="implementation output",
            )


def test_refresh_workspace_resets_to_recorded_base(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with (
        patch("spec_kitty_orchestrator.loop._workspace_metadata_base_commit", return_value="base-commit"),
        patch("spec_kitty_orchestrator.loop._run_command") as run_command,
        patch("spec_kitty_orchestrator.loop._git_status_lines", return_value=[]),
        patch("spec_kitty_orchestrator.loop._git_head", return_value="base-commit"),
    ):
        run_command.side_effect = [
            Mock(returncode=0, stderr="", stdout=""),
            Mock(returncode=0, stderr="", stdout=""),
        ]

        refreshed = _refresh_workspace(tmp_path, "010-test-feature", "WP01", workspace)

    assert refreshed == workspace
    reset_cmd = run_command.call_args_list[0].args[0]
    clean_cmd = run_command.call_args_list[1].args[0]
    assert reset_cmd == ["git", "reset", "--hard", "base-commit"]
    assert clean_cmd == ["git", "clean", "-fd"]


def test_ensure_workspace_exists_refreshes_stale_workspace_without_reusing_host_implement(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with (
        patch("spec_kitty_orchestrator.loop._workspace_requires_rebootstrap", return_value=True),
        patch("spec_kitty_orchestrator.loop._refresh_workspace", return_value=workspace) as refresh_workspace,
        patch("spec_kitty_orchestrator.loop._run_command") as run_command,
    ):
        ensured = _ensure_workspace_exists(tmp_path, "010-test-feature", "WP01", workspace)

    assert ensured == workspace
    refresh_workspace.assert_called_once_with(tmp_path, "010-test-feature", "WP01", workspace)
    run_command.assert_not_called()
