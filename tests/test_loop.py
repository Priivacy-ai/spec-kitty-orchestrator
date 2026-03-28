from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from spec_kitty_orchestrator.config import OrchestratorConfig
from spec_kitty_orchestrator.host.client import TransitionRejectedError
from spec_kitty_orchestrator.loop import (
    OrchestrationError,
    _build_review_prompt,
    execute_and_advance,
    _run_review_phase,
    _execute_agent_or_block,
    _finalize_successful_implementation,
    _ensure_workspace_exists,
    _recoverable_work_packages,
    _refresh_workspace,
    _resolve_wp_prompt_path,
    _sanitize_prompt_paths,
    _workspace_requires_rebootstrap,
    run_orchestration_loop,
)
from spec_kitty_orchestrator.policy import PolicyMetadata
from spec_kitty_orchestrator.state import new_run_state


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

    with (
        patch("spec_kitty_orchestrator.loop._git_status_lines", return_value=[]),
        patch("spec_kitty_orchestrator.loop._git_head", return_value="same-head"),
    ):
        with pytest.raises(OrchestrationError, match="without any worktree changes or commits"):
            _finalize_successful_implementation(
                workspace_path=workspace,
                wp_id="WP01",
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


def test_ensure_workspace_exists_reports_all_bootstrap_attempt_errors(tmp_path: Path) -> None:
    workspace = tmp_path / ".worktrees" / "010-test-feature-WP08"

    with (
        patch("spec_kitty_orchestrator.loop._run_command") as run_command,
    ):
        run_command.side_effect = [
            Mock(
                returncode=1,
                stdout='{"status":"error","error":"force bootstrap failed"}',
                stderr="",
            ),
            Mock(
                returncode=1,
                stdout='{"status":"error","error":"dependencies should be merged first"}',
                stderr="",
            ),
        ]

        with pytest.raises(OrchestrationError, match="force bootstrap failed") as exc_info:
            _ensure_workspace_exists(tmp_path, "010-test-feature", "WP08", workspace)

    message = str(exc_info.value)
    assert "implement WP08 --feature 010-test-feature --force" in message
    assert "implement WP08 --feature 010-test-feature" in message
    assert "dependencies should be merged first" in message


def test_execute_and_advance_blocks_when_prompt_load_fails(tmp_path: Path) -> None:
    cfg = OrchestratorConfig(repo_root=tmp_path, actor="spec-kitty-orchestrator")
    run_state = new_run_state(
        "010-test-feature",
        PolicyMetadata(
            orchestrator_id="spec-kitty-orchestrator",
            orchestrator_version="0.1.0",
            agent_family="qwen",
            approval_mode="full_auto",
            sandbox_mode="workspace_write",
            network_mode="open",
            dangerous_flags=["--yolo"],
        ),
    )
    host = Mock()
    concurrency = Mock()

    with patch(
        "spec_kitty_orchestrator.loop._load_prompt_text",
        side_effect=OrchestrationError("prompt missing"),
    ):
        asyncio.run(
            execute_and_advance(
                wp_id="WP01",
                feature="010-test-feature",
                workspace_path=tmp_path,
                prompt_path=tmp_path / "WP01.md",
                impl_agent_id="qwen",
                host=host,
                run_state=run_state,
                agent_cfg=cfg.agent_selection,
                cfg=cfg,
                concurrency=concurrency,
            )
        )

    host.append_history.assert_called_once_with(
        "010-test-feature",
        "WP01",
        "FAILED: prompt missing",
    )
    host.transition.assert_called_once_with(
        "010-test-feature",
        "WP01",
        "blocked",
        note="prompt missing",
    )


def test_run_review_phase_blocks_when_start_review_rejected(tmp_path: Path) -> None:
    cfg = OrchestratorConfig(repo_root=tmp_path, actor="spec-kitty-orchestrator")
    run_state = new_run_state(
        "010-test-feature",
        PolicyMetadata(
            orchestrator_id="spec-kitty-orchestrator",
            orchestrator_version="0.1.0",
            agent_family="qwen",
            approval_mode="full_auto",
            sandbox_mode="workspace_write",
            network_mode="open",
            dangerous_flags=["--yolo"],
        ),
    )
    host = Mock()
    host.start_review.side_effect = TransitionRejectedError("TRANSITION_REJECTED", "host rejected restart")
    review_result = Mock()

    with (
        patch("spec_kitty_orchestrator.loop.select_reviewer", return_value="qwen"),
        patch("spec_kitty_orchestrator.loop._execute_agent_or_block", return_value=review_result),
        patch("spec_kitty_orchestrator.loop.is_review_approved", return_value=False),
        patch("spec_kitty_orchestrator.loop.extract_review_feedback", return_value="needs changes"),
        patch("spec_kitty_orchestrator.loop.extract_review_verdict", return_value="REJECTED"),
    ):
        asyncio.run(
            _run_review_phase(
                wp_id="WP01",
                feature="010-test-feature",
                workspace_path=tmp_path,
                prompt_text="prompt",
                impl_agent_id="qwen",
                baseline_head="abc123",
                host=host,
                run_state=run_state,
                agent_cfg=cfg.agent_selection,
                cfg=cfg,
            )
        )

    assert "Review rejection could not restart implementation" in run_state.get_or_create_wp("WP01").last_error
    host.transition.assert_called_once()
    assert host.transition.call_args.args[:3] == ("010-test-feature", "WP01", "blocked")


def test_run_orchestration_loop_blocks_recovery_workspace_failures(tmp_path: Path) -> None:
    cfg = OrchestratorConfig(repo_root=tmp_path, actor="spec-kitty-orchestrator")
    run_state = new_run_state(
        "010-test-feature",
        PolicyMetadata(
            orchestrator_id="spec-kitty-orchestrator",
            orchestrator_version="0.1.0",
            agent_family="qwen",
            approval_mode="full_auto",
            sandbox_mode="workspace_write",
            network_mode="open",
            dangerous_flags=["--yolo"],
        ),
    )
    run_state.get_or_create_wp("WP01").implementation_agent = "qwen"
    host = Mock()
    host.list_ready.side_effect = [
        SimpleNamespace(ready_work_packages=[]),
        SimpleNamespace(ready_work_packages=[]),
    ]
    host.feature_state.side_effect = [
        SimpleNamespace(work_packages=[SimpleNamespace(wp_id="WP01", lane="in_progress")]),
        SimpleNamespace(work_packages=[SimpleNamespace(wp_id="WP01", lane="blocked")]),
    ]
    host.start_implementation.return_value = SimpleNamespace(
        workspace_path=str(tmp_path / ".worktrees" / "010-test-feature-WP01"),
        prompt_path=str(tmp_path / "WP01.md"),
    )

    with patch(
        "spec_kitty_orchestrator.loop._ensure_workspace_exists",
        side_effect=OrchestrationError("workspace boom"),
    ):
        asyncio.run(
            run_orchestration_loop(
                "010-test-feature",
                host,
                run_state,
                cfg,
            )
        )

    host.append_history.assert_called_once_with(
        "010-test-feature",
        "WP01",
        "FAILED: Recovery workspace preparation failed: workspace boom",
    )
    host.transition.assert_called_once_with(
        "010-test-feature",
        "WP01",
        "blocked",
        note="Recovery workspace preparation failed: workspace boom",
    )


def test_build_review_prompt_disallows_implementation() -> None:
    review_prompt = _build_review_prompt("Original WP prompt", Path("/tmp/workspace"))

    assert review_prompt.startswith("## Review Assignment")
    assert "Do not modify files" in review_prompt
    assert "Do not modify files, write code, or produce an implementation summary." in review_prompt
    assert "VERDICT: APPROVED" in review_prompt
    assert "VERDICT: REJECTED" in review_prompt
    assert "reference material only" in review_prompt
    assert review_prompt.endswith("Original WP prompt\n")


def test_resolve_wp_prompt_path_supports_suffixed_task_filenames(tmp_path: Path) -> None:
    feature_dir = tmp_path / "kitty-specs" / "010-test-feature" / "tasks"
    feature_dir.mkdir(parents=True)
    prompt_path = feature_dir / "WP04-runtime-action-flows.md"
    prompt_path.write_text("content", encoding="utf-8")

    resolved = _resolve_wp_prompt_path(tmp_path, "010-test-feature", "WP04")

    assert resolved == prompt_path


def test_recoverable_work_packages_only_returns_tracked_in_progress_and_for_review() -> None:
    concurrency = Mock()
    concurrency.is_active.return_value = False

    run_state = Mock()
    run_state.wp_executions = {"WP02": Mock(), "WP03": Mock()}

    feature_state = [
        Mock(wp_id="WP01", lane="planned"),
        Mock(wp_id="WP02", lane="in_progress"),
        Mock(wp_id="WP03", lane="for_review"),
        Mock(wp_id="WP04", lane="done"),
    ]

    recoverable = _recoverable_work_packages(feature_state, {"WP01"}, run_state, concurrency)

    assert [wp.wp_id for wp in recoverable] == ["WP02", "WP03"]


def test_execute_agent_or_block_marks_wp_blocked_on_process_crash(tmp_path: Path) -> None:
    cfg = OrchestratorConfig(repo_root=tmp_path, actor="spec-kitty-orchestrator")
    run_state = new_run_state(
        "010-test-feature",
        PolicyMetadata(
            orchestrator_id="spec-kitty-orchestrator",
            orchestrator_version="0.1.0",
            agent_family="qwen",
            approval_mode="full_auto",
            sandbox_mode="workspace_write",
            network_mode="open",
            dangerous_flags=["--yolo"],
        ),
    )
    wp_exec = run_state.get_or_create_wp("WP04")
    host = Mock()
    invoker = Mock()
    invoker.agent_id = "qwen"

    with patch(
        "spec_kitty_orchestrator.loop.execute_agent",
        side_effect=RuntimeError("spawn failed"),
    ):
        result = asyncio.run(
            _execute_agent_or_block(
                invoker=invoker,
                prompt="prompt",
                working_dir=tmp_path,
                role="review",
                timeout_seconds=60,
                log_file=tmp_path / "review.log",
                failure_label="review",
                feature="010-test-feature",
                wp_id="WP04",
                wp_exec=wp_exec,
                host=host,
                run_state=run_state,
                cfg=cfg,
            )
        )

    assert result is None
    assert wp_exec.last_error == "review task crashed: spawn failed"
    host.append_history.assert_called_once_with(
        "010-test-feature",
        "WP04",
        "FAILED: review task crashed: spawn failed",
    )
    host.transition.assert_called_once_with(
        "010-test-feature",
        "WP04",
        "blocked",
        note="review task crashed: spawn failed",
    )
