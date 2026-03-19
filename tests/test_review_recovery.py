from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from spec_kitty_orchestrator.agents.base import InvocationResult
from spec_kitty_orchestrator.config import AgentSelectionConfig, OrchestratorConfig
from spec_kitty_orchestrator.host.client import TransitionRejectedError
from spec_kitty_orchestrator.loop import run_orchestration_loop
from spec_kitty_orchestrator.policy import PolicyMetadata
from spec_kitty_orchestrator.state import RunState


@dataclass
class _WorkPackage:
    wp_id: str
    lane: str
    dependencies: list[str]


class _RejectedHandoffHost:
    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root
        self.lane = "planned"
        self.transitions: list[tuple[str, str, str | None, str | None]] = []
        self.history: list[str] = []
        self.move_attempts = 0
        self.marked_subtasks: list[tuple[str, ...]] = []

    def append_history(self, feature: str, wp: str, note: str) -> None:
        self.history.append(note)

    def list_ready(self, feature: str) -> SimpleNamespace:
        ready = []
        if self.lane == "planned":
            ready = [_WorkPackage(wp_id="WP01", lane="planned", dependencies=[])]
        return SimpleNamespace(feature_slug=feature, ready_work_packages=ready)

    def feature_state(self, feature: str) -> SimpleNamespace:
        return SimpleNamespace(
            feature_slug=feature,
            summary={self.lane: 1},
            work_packages=[_WorkPackage(wp_id="WP01", lane=self.lane, dependencies=[])],
        )

    def start_implementation(self, feature: str, wp: str) -> SimpleNamespace:
        self.lane = "in_progress"
        return SimpleNamespace(
            feature_slug=feature,
            wp_id=wp,
            from_lane="planned",
            to_lane="in_progress",
            workspace_path=str(self.repo_root / ".worktrees" / "004-aegis-live-runtime-mvp-WP01"),
            prompt_path=str(
                self.repo_root
                / "kitty-specs"
                / "004-aegis-live-runtime-mvp"
                / "tasks"
                / "WP01-canonical-demo-adapter-foundation.md"
            ),
            policy_metadata_recorded=True,
            no_op=False,
        )

    def move_task_for_review(
        self,
        feature: str,
        wp: str,
        agent: str,
        note: str | None = None,
    ) -> dict:
        self.move_attempts += 1
        raise TransitionRejectedError(
            "TRANSITION_REJECTED",
            "Cannot move WP01 to for_review - unchecked subtasks",
        )

    def mark_task_status(self, feature: str, task_ids: list[str], status: str) -> dict:
        self.marked_subtasks.append(tuple(task_ids))
        return {"result": "success", "updated": task_ids, "status": status}

    def transition(
        self,
        feature: str,
        wp: str,
        to: str,
        note: str | None = None,
        review_ref: str | None = None,
    ) -> SimpleNamespace:
        self.transitions.append((wp, to, note, review_ref))
        self.lane = to
        return SimpleNamespace(
            feature_slug=feature,
            wp_id=wp,
            from_lane="in_progress",
            to_lane=to,
            policy_metadata_recorded=True,
        )


class _ReconcileThenReviewHost(_RejectedHandoffHost):
    def move_task_for_review(
        self,
        feature: str,
        wp: str,
        agent: str,
        note: str | None = None,
    ) -> dict:
        self.move_attempts += 1
        self.lane = "for_review"
        return {"result": "success", "task_id": wp, "new_lane": "for_review"}


def _make_run_state() -> RunState:
    policy = PolicyMetadata(
        orchestrator_id="spec-kitty-orchestrator",
        orchestrator_version="0.1.0",
        agent_family="claude",
        approval_mode="full_auto",
        sandbox_mode="workspace_write",
        network_mode="none",
        dangerous_flags=[],
    )
    return RunState(
        run_id="run-123",
        feature_slug="004-aegis-live-runtime-mvp",
        started_at="2026-03-19T00:00:00+00:00",
        policy=policy,
    )


def _make_cfg(repo_root: Path) -> OrchestratorConfig:
    cfg = OrchestratorConfig(repo_root=repo_root, actor="spec-kitty-orchestrator")
    cfg.agent_selection = AgentSelectionConfig(
        implementation_agents=["gemini"],
        review_agents=["gemini"],
        timeout_seconds=1,
        max_retries=0,
    )
    return cfg


def _write_prompt(repo_root: Path) -> None:
    task_dir = repo_root / "kitty-specs" / "004-aegis-live-runtime-mvp" / "tasks"
    task_dir.mkdir(parents=True)
    (repo_root / ".worktrees" / "004-aegis-live-runtime-mvp-WP01").mkdir(parents=True)
    prompt_path = task_dir / "WP01-canonical-demo-adapter-foundation.md"
    prompt_path.write_text(
        "---\n"
        "subtasks:\n"
        "  - T006\n"
        "  - T007\n"
        "---\n"
        "# prompt\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_handoff_rejection_marks_subtasks_before_blocking(tmp_path: Path) -> None:
    repo_root = tmp_path
    _write_prompt(repo_root)
    host = _RejectedHandoffHost(repo_root)
    run_state = _make_run_state()
    cfg = _make_cfg(repo_root)

    result = InvocationResult(
        success=True,
        exit_code=0,
        stdout='{"result":"ok"}\n',
        stderr="",
        duration_seconds=0.01,
    )

    with patch("spec_kitty_orchestrator.loop.execute_agent", new=AsyncMock(return_value=result)), \
         patch("spec_kitty_orchestrator.loop.get_invoker", return_value=MagicMock(agent_id="gemini")), \
         patch("spec_kitty_orchestrator.loop.LOOP_POLL_INTERVAL", 0.0):
        await run_orchestration_loop("004-aegis-live-runtime-mvp", host, run_state, cfg)

    assert host.marked_subtasks == [("T006", "T007")]
    assert host.move_attempts == 1
    assert any(to == "blocked" for _, to, _, _ in host.transitions)


@pytest.mark.asyncio
async def test_successful_impl_reconciles_subtasks_before_review_handoff(tmp_path: Path) -> None:
    repo_root = tmp_path
    _write_prompt(repo_root)
    host = _ReconcileThenReviewHost(repo_root)
    run_state = _make_run_state()
    cfg = _make_cfg(repo_root)

    impl_result = InvocationResult(
        success=True,
        exit_code=0,
        stdout='{"result":"ok"}\n',
        stderr="",
        duration_seconds=0.01,
    )
    review_result = InvocationResult(
        success=True,
        exit_code=0,
        stdout='{"review":"approved"}\n',
        stderr="",
        duration_seconds=0.01,
    )

    with patch(
        "spec_kitty_orchestrator.loop.execute_agent",
        new=AsyncMock(side_effect=[impl_result, review_result]),
    ), patch("spec_kitty_orchestrator.loop.get_invoker", return_value=MagicMock(agent_id="gemini")), \
        patch("spec_kitty_orchestrator.loop.LOOP_POLL_INTERVAL", 0.0):
        await run_orchestration_loop("004-aegis-live-runtime-mvp", host, run_state, cfg)

    assert host.marked_subtasks[0] == ("T006", "T007")
    assert host.move_attempts == 1
    assert host.lane == "done"
