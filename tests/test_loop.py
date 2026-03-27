from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from spec_kitty_orchestrator.loop import (
    OrchestrationError,
    _finalize_successful_implementation,
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
