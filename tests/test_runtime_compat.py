from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from spec_kitty_orchestrator.executor import ensure_working_dir
from spec_kitty_orchestrator.host.client import TransitionRejectedError
from spec_kitty_orchestrator.loop import (
    _transition_for_review,
    _transition_review_approved,
    _transition_review_rejected,
)


def run_git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def test_ensure_working_dir_creates_missing_git_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    run_git(repo, "init", "-b", "main")
    run_git(repo, "config", "user.email", "test@example.test")
    run_git(repo, "config", "user.name", "Runtime Compat Test")
    (repo / "README.md").write_text("# repo\n", encoding="utf-8")
    run_git(repo, "add", "README.md")
    run_git(repo, "commit", "-m", "init")

    worktree = repo / ".worktrees" / "099-feature-WP01"
    ensure_working_dir(worktree, repo_root=repo)

    assert (worktree / ".git").exists()
    run_git(worktree, "status", "--porcelain")


class ReviewLaneHost:
    def __init__(
        self,
        lane: str = "for_review",
        reject_first_for_review: bool = False,
    ) -> None:
        self.lane = lane
        self.reject_first_for_review = reject_first_for_review
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def start_review(
        self,
        mission: str,
        wp_id: str,
        review_ref: str,
    ) -> SimpleNamespace:
        self.calls.append(("start_review", review_ref, {}))
        if self.lane != "for_review":
            raise TransitionRejectedError(
                "TRANSITION_REJECTED",
                f"cannot start review from {self.lane}",
            )
        self.lane = "in_review"
        return SimpleNamespace(to_lane="in_review")

    def transition(
        self,
        mission: str,
        wp_id: str,
        to: str,
        **kwargs: Any,
    ) -> SimpleNamespace:
        self.calls.append(("transition", to, kwargs))
        if to == "for_review" and self.reject_first_for_review:
            self.reject_first_for_review = False
            raise TransitionRejectedError("TRANSITION_REJECTED", "strict for_review guard")
        if to == "done" and self.lane == "for_review":
            raise TransitionRejectedError(
                "TRANSITION_REJECTED",
                "for_review cannot go directly to done",
            )
        self.lane = to
        return SimpleNamespace(to_lane=to)


def test_review_approval_moves_current_in_review_lane_to_done() -> None:
    host = ReviewLaneHost(lane="in_review")

    _transition_review_approved(host, "099-feature", "WP01", "codex", 1, "review-ref")

    assert host.lane == "done"
    assert host.calls == [
        (
            "transition",
            "done",
            {
                "note": "Review approved by 'codex'",
                "review_ref": "review-ref",
                "force": True,
            },
        )
    ]


def test_review_rejection_moves_current_in_review_lane_back_to_in_progress() -> None:
    host = ReviewLaneHost(lane="in_review")

    _transition_review_rejected(host, "099-feature", "WP01", "feedback-ref")

    assert host.lane == "in_progress"
    assert host.calls == [
        (
            "transition",
            "in_progress",
            {
                "note": "Review rejected; rework required",
                "review_ref": "feedback-ref",
            },
        ),
    ]


def test_review_rejection_retries_with_force_when_strict_guards_reject() -> None:
    class StrictReviewHost(ReviewLaneHost):
        def transition(self, mission: str, wp_id: str, to: str, **kwargs: Any) -> SimpleNamespace:
            self.calls.append(("transition", to, kwargs))
            if to == "in_progress" and not kwargs.get("force"):
                raise TransitionRejectedError("TRANSITION_REJECTED", "strict rework guard")
            self.lane = to
            return SimpleNamespace(to_lane=to)

    host = StrictReviewHost(lane="in_review")

    _transition_review_rejected(host, "099-feature", "WP01", "feedback-ref")

    assert host.lane == "in_progress"
    assert host.calls == [
        (
            "transition",
            "in_progress",
            {
                "note": "Review rejected; rework required",
                "review_ref": "feedback-ref",
            },
        ),
        (
            "transition",
            "in_progress",
            {
                "note": "Review rejected; rework required",
                "review_ref": "feedback-ref",
                "force": True,
            },
        ),
    ]


def test_review_approval_does_not_claim_review_again() -> None:
    host = ReviewLaneHost(lane="in_review")

    _transition_review_approved(host, "099-feature", "WP01", "codex", 1, "review-ref")

    assert host.calls == [
        (
            "transition",
            "done",
            {
                "note": "Review approved by 'codex'",
                "review_ref": "review-ref",
                "force": True,
            },
        )
    ]


def test_for_review_transition_retries_with_force_when_strict_guards_reject() -> None:
    host = ReviewLaneHost(lane="in_progress", reject_first_for_review=True)

    _transition_for_review(host, "099-feature", "WP01", "implementation complete")

    assert host.lane == "for_review"
    assert host.calls[0][2]["subtasks_complete"] is True
    assert host.calls[0][2]["implementation_evidence_present"] is True
    assert host.calls[1][2]["force"] is True
