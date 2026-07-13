"""Tests for resuming orphaned in_progress WPs instead of false deadlock.

Background: list-ready never returns a claimed/in_progress WP. So a WP left in
one of those lanes by a prior/interrupted run (or an out-of-band
start-implementation, e.g. host-side testing) was invisible to the loop: it
scheduled nothing, polled empty, and raised a misleading "Dependency deadlock"
even though the WP just needed to be picked back up.

These tests pin the fix: the loop adopts orphaned resumable WPs, and only
reports a stall when there is genuinely nothing it can drive.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from spec_kitty_orchestrator import loop as loop_mod
from spec_kitty_orchestrator.config import load_config
from spec_kitty_orchestrator.host.client import WPAlreadyClaimedError
from spec_kitty_orchestrator.loop import DeadlockError, run_orchestration_loop
from spec_kitty_orchestrator.policy import PolicyMetadata
from spec_kitty_orchestrator.scheduler import select_schedulable_wp_ids
from spec_kitty_orchestrator.state import new_run_state


def _ready(wp_id: str):
    return SimpleNamespace(wp_id=wp_id)


def _state(wp_id: str, lane: str, last_actor: str | None = None):
    return SimpleNamespace(
        wp_id=wp_id, lane=lane, dependencies=[], last_actor=last_actor
    )


# -- pure scheduling decision -------------------------------------------------


class TestSelectSchedulable:
    def test_ready_wps_are_scheduled(self) -> None:
        ids = select_schedulable_wp_ids([_ready("WP01")], [], set(), set())
        assert ids == ["WP01"]

    def test_orphaned_in_progress_is_adopted(self) -> None:
        # The core bug: list-ready is empty, but a WP is stuck in_progress.
        ids = select_schedulable_wp_ids([], [_state("WP01", "in_progress")], set(), set())
        assert ids == ["WP01"]

    def test_orphaned_claimed_is_adopted(self) -> None:
        ids = select_schedulable_wp_ids([], [_state("WP01", "claimed")], set(), set())
        assert ids == ["WP01"]

    def test_active_wp_is_not_rescheduled(self) -> None:
        ids = select_schedulable_wp_ids([], [_state("WP01", "in_progress")], {"WP01"}, set())
        assert ids == []

    def test_already_driven_orphan_is_not_readopted(self) -> None:
        # A WP this process already drove that failed back/stuck in_progress must
        # not be re-adopted into an infinite loop.
        ids = select_schedulable_wp_ids([], [_state("WP01", "in_progress")], set(), {"WP01"})
        assert ids == []

    def test_already_driven_ready_wp_is_not_readopted(self) -> None:
        # A ready/planned WP whose allocation failed must also be quarantined.
        ids = select_schedulable_wp_ids([_ready("WP01")], [], set(), {"WP01"})
        assert ids == []

    def test_orphaned_for_review_is_adopted(self) -> None:
        # Defect 3: a for_review WP left by an interrupted run must be adopted so a
        # reviewer is dispatched on resume (routed review-only by the loop).
        ids = select_schedulable_wp_ids([], [_state("WP01", "for_review")], set(), set())
        assert ids == ["WP01"]

    def test_non_resumable_lanes_are_ignored(self) -> None:
        # planned is surfaced via list-ready (not orphan-adopted); blocked/done are
        # genuinely non-resumable by the scheduler. in_review is recovered to
        # for_review by _recover_orphaned_reviews before the scheduler runs.
        wps = [_state("WP01", "planned"), _state("WP02", "blocked"), _state("WP03", "done")]
        assert select_schedulable_wp_ids([], wps, set(), set()) == []

    def test_dedup_ready_and_state(self) -> None:
        ids = select_schedulable_wp_ids([_ready("WP01")], [_state("WP01", "in_progress")], set(), set())
        assert ids == ["WP01"]


# -- loop-level regression ----------------------------------------------------


class _FakeHost:
    """Minimal HostClient stand-in. WP01 starts orphaned in_progress."""

    def __init__(self) -> None:
        self.actor = "spec-kitty-orchestrator"
        self.lanes = {"WP01": "in_progress"}
        self.start_impl_calls: list[str] = []
        self.last_actors: dict[str, str | None] = {"WP01": None}

    def list_ready(self, mission):  # in_progress WP is never "ready"
        return SimpleNamespace(ready_work_packages=[])

    def mission_state(self, mission):
        return SimpleNamespace(
            work_packages=[
                _state(k, v, self.last_actors.get(k)) for k, v in self.lanes.items()
            ]
        )

    def start_implementation(self, mission, wp):
        self.start_impl_calls.append(wp)
        return SimpleNamespace(
            workspace_path="/tmp/ws", prompt_path="/tmp/p.md",
            lane_branch=None, lane_base_ref=None,
        )


def _cfg(tmp_path: Path):
    (tmp_path / ".kittify").mkdir()
    return load_config(tmp_path, "spec-kitty-orchestrator")


def _policy() -> PolicyMetadata:
    return PolicyMetadata(
        orchestrator_id="spec-kitty-orchestrator",
        orchestrator_version="0.1.2",
        agent_family="claude",
        approval_mode="full_auto",
        sandbox_mode="workspace_write",
        network_mode="none",
        dangerous_flags=[],
        tool_restrictions=None,
    )


def test_loop_adopts_orphaned_in_progress_instead_of_deadlock(tmp_path, monkeypatch) -> None:
    """The loop must pick up an orphaned in_progress WP and drive it, not raise
    a false DeadlockError. Fails on the pre-fix loop (which ignored in_progress
    WPs and dead-locked)."""
    monkeypatch.setattr(loop_mod, "LOOP_POLL_INTERVAL", 0.001)

    host = _FakeHost()

    async def fake_execute_and_advance(wp_id, mission, ws, pp, agent, h, rs, ac, cfg, conc, **kwargs):
        # Simulate the implementer running and the WP reaching a terminal lane.
        h.lanes[wp_id] = "done"

    monkeypatch.setattr(loop_mod, "execute_and_advance", fake_execute_and_advance)

    cfg = _cfg(tmp_path)
    run_state = new_run_state("m", _policy())

    async def run():
        await asyncio.wait_for(
            run_orchestration_loop("m", host, run_state, cfg), timeout=5.0
        )

    asyncio.run(run())  # must not raise DeadlockError

    assert host.start_impl_calls == ["WP01"], "orphaned WP01 should have been adopted"
    assert host.lanes["WP01"] == "done"


def test_loop_reports_stuck_wps_when_genuinely_blocked(tmp_path, monkeypatch) -> None:
    """When nothing is schedulable/resumable and not all terminal, the stall
    error names the stuck WP and its lane (no longer an opaque 'deadlock')."""
    monkeypatch.setattr(loop_mod, "LOOP_POLL_INTERVAL", 0.001)
    monkeypatch.setattr(loop_mod, "DEADLOCK_THRESHOLD", 2)

    class StuckHost(_FakeHost):
        def __init__(self) -> None:
            super().__init__()
            # WP01 stuck in approved — non-resumable (not in RESUMABLE_LANES) and
            # non-terminal (not in terminal_lanes), so the loop cannot make progress.
            # The orchestrator drives in_review -> done directly; a WP stranded in
            # approved represents a state the orchestrator cannot recover from.
            self.lanes = {"WP01": "approved"}

    host = StuckHost()
    cfg = _cfg(tmp_path)
    run_state = new_run_state("m", _policy())

    async def run():
        await asyncio.wait_for(
            run_orchestration_loop("m", host, run_state, cfg), timeout=5.0
        )

    with pytest.raises(DeadlockError) as exc:
        asyncio.run(run())

    assert "WP01" in str(exc.value)
    assert "approved" in str(exc.value)
    assert host.start_impl_calls == [], "non-resumable WP must not be (re)started"


def test_loop_resumes_for_review_wp_via_review(tmp_path, monkeypatch) -> None:
    """Defect 3: a WP parked in for_review by an interrupted run is adopted and
    driven REVIEW-only on resume — resolved read-only (never start-implementation),
    with current_lane='for_review' so execute_and_advance skips implementation."""
    monkeypatch.setattr(loop_mod, "LOOP_POLL_INTERVAL", 0.001)

    class ReviewHost(_FakeHost):
        def __init__(self) -> None:
            super().__init__()
            self.lanes = {"WP01": "for_review"}
            self.resolve_calls: list[str] = []

        def resolve_workspace(self, mission, wp):
            self.resolve_calls.append(wp)
            return SimpleNamespace(
                workspace_path="/tmp/ws", prompt_path="/tmp/p.md",
                lane_branch=None, lane_id=None, lane_base_ref=None,
            )

    host = ReviewHost()
    captured: dict = {}

    async def fake_execute_and_advance(wp_id, mission, ws, pp, agent, h, rs, ac, cfg, conc, **kwargs):
        captured["current_lane"] = kwargs.get("current_lane")
        h.lanes[wp_id] = "done"  # reviewer approved

    monkeypatch.setattr(loop_mod, "execute_and_advance", fake_execute_and_advance)

    cfg = _cfg(tmp_path)
    run_state = new_run_state("m", _policy())

    async def run():
        await asyncio.wait_for(
            run_orchestration_loop("m", host, run_state, cfg), timeout=5.0
        )

    asyncio.run(run())  # must not raise DeadlockError

    assert host.resolve_calls == ["WP01"], "for_review WP resolved read-only"
    assert host.start_impl_calls == [], "must NOT start-implementation a for_review WP"
    assert captured["current_lane"] == "for_review", "review-only dispatch"
    assert host.lanes["WP01"] == "done"


def test_start_implementation_failure_quarantines_wp_no_infinite_loop(
    tmp_path, monkeypatch
) -> None:
    """A WP whose start-implementation fails (e.g. LANE_ALLOCATION_FAILED on a
    stale dirty lane) must be quarantined after ONE attempt — not re-adopted every
    poll into a tight loop. It surfaces as a stall with its last_error instead."""
    monkeypatch.setattr(loop_mod, "LOOP_POLL_INTERVAL", 0.001)
    monkeypatch.setattr(loop_mod, "DEADLOCK_THRESHOLD", 2)

    class FailingHost(_FakeHost):
        def start_implementation(self, mission, wp):
            self.start_impl_calls.append(wp)
            raise RuntimeError("[LANE_ALLOCATION_FAILED] lane worktree has uncommitted changes")

    host = FailingHost()  # WP01 orphaned in_progress (from _FakeHost)
    cfg = _cfg(tmp_path)
    run_state = new_run_state("m", _policy())

    async def run():
        await asyncio.wait_for(
            run_orchestration_loop("m", host, run_state, cfg), timeout=5.0
        )

    with pytest.raises(DeadlockError) as exc:
        asyncio.run(run())

    # Exactly ONE start-implementation attempt — quarantined, never re-adopted.
    assert host.start_impl_calls == ["WP01"], (
        f"expected a single attempt (no infinite loop), got {host.start_impl_calls}"
    )
    # The stall report surfaces the offending WP (with its last_error).
    assert "WP01" in str(exc.value)


def test_ready_start_implementation_failure_quarantines_wp_no_infinite_loop(
    tmp_path, monkeypatch
) -> None:
    """A planned/ready WP whose start-implementation fails must be quarantined too.

    Regression: driven_ids was applied only to orphaned state WPs, so list-ready
    reselected the failed planned WP forever and the loop never reached stall
    reporting.
    """
    monkeypatch.setattr(loop_mod, "LOOP_POLL_INTERVAL", 0.001)
    monkeypatch.setattr(loop_mod, "DEADLOCK_THRESHOLD", 2)

    class ReadyFailingHost(_FakeHost):
        def __init__(self) -> None:
            super().__init__()
            self.lanes = {"WP01": "planned"}

        def list_ready(self, mission):
            return SimpleNamespace(ready_work_packages=[_ready("WP01")])

        def start_implementation(self, mission, wp):
            self.start_impl_calls.append(wp)
            raise RuntimeError("[LANE_ALLOCATION_FAILED] lane worktree has uncommitted changes")

    host = ReadyFailingHost()
    cfg = _cfg(tmp_path)
    run_state = new_run_state("m", _policy())

    async def run():
        await asyncio.wait_for(
            run_orchestration_loop("m", host, run_state, cfg), timeout=5.0
        )

    with pytest.raises(DeadlockError) as exc:
        asyncio.run(run())

    assert host.start_impl_calls == ["WP01"]
    assert "WP01" in str(exc.value)


def test_loop_recovers_in_review_wp_on_restart(tmp_path, monkeypatch) -> None:
    """Fix for #28: a WP stuck in in_review (reviewer killed mid-flight) must be
    force-transitioned back to for_review on startup so a fresh reviewer is
    dispatched, rather than stalling immediately with a deadlock error."""
    monkeypatch.setattr(loop_mod, "LOOP_POLL_INTERVAL", 0.001)

    class InReviewHost(_FakeHost):
        def __init__(self) -> None:
            super().__init__()
            self.lanes = {"WP01": "in_review"}
            self.last_actors = {"WP01": self.actor}
            self.transition_calls: list[tuple] = []
            self.resolve_calls: list[str] = []
            self.append_history_calls: list = []

        def transition(self, mission, wp, to, *, note=None, force=False, **_kwargs):
            self.transition_calls.append((wp, to, force))
            self.lanes[wp] = to

        def resolve_workspace(self, mission, wp):
            self.resolve_calls.append(wp)
            return SimpleNamespace(
                workspace_path="/tmp/ws", prompt_path="/tmp/p.md",
                lane_branch=None, lane_id=None, lane_base_ref=None,
            )

        def append_history(self, mission, wp, message):
            self.append_history_calls.append((wp, message))

    host = InReviewHost()
    captured: dict = {}

    async def fake_execute_and_advance(wp_id, mission, ws, pp, agent, h, rs, ac, cfg, conc, **kwargs):
        captured["current_lane"] = kwargs.get("current_lane")
        h.lanes[wp_id] = "done"

    monkeypatch.setattr(loop_mod, "execute_and_advance", fake_execute_and_advance)

    cfg = _cfg(tmp_path)
    run_state = new_run_state("m", _policy())

    async def run():
        await asyncio.wait_for(
            run_orchestration_loop(
                "m", host, run_state, cfg, recover_in_review=True
            ),
            timeout=5.0,
        )

    asyncio.run(run())  # must not raise DeadlockError

    # Recovery: in_review was force-transitioned back to for_review
    assert any(
        wp == "WP01" and to == "for_review" and force
        for wp, to, force in host.transition_calls
    ), f"expected forced in_review->for_review recovery, got {host.transition_calls}"
    # Then treated as a for_review orphan (resolved read-only, not start-implementation)
    assert host.resolve_calls == ["WP01"], "recovered WP resolved read-only"
    assert host.start_impl_calls == [], "must not start-implementation a recovered in_review WP"
    assert captured["current_lane"] == "for_review"
    assert host.lanes["WP01"] == "done"


def test_loop_does_not_recover_review_owned_by_another_actor(
    tmp_path, monkeypatch
) -> None:
    """A live/external review identity must never be force-rewound."""
    monkeypatch.setattr(loop_mod, "LOOP_POLL_INTERVAL", 0.001)
    monkeypatch.setattr(loop_mod, "DEADLOCK_THRESHOLD", 2)

    class ExternalReviewHost(_FakeHost):
        def __init__(self) -> None:
            super().__init__()
            self.lanes = {"WP01": "in_review"}
            self.last_actors = {"WP01": "human-reviewer"}
            self.transition_calls: list[tuple] = []

        def transition(self, mission, wp, to, **kwargs):
            self.transition_calls.append((wp, to, kwargs))

    host = ExternalReviewHost()
    cfg = _cfg(tmp_path)
    run_state = new_run_state("m", _policy())

    with pytest.raises(DeadlockError):
        asyncio.run(
            run_orchestration_loop(
                "m", host, run_state, cfg, recover_in_review=True
            )
        )

    assert host.transition_calls == []


def test_recovery_failure_surfaces_original_error(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(loop_mod, "LOOP_POLL_INTERVAL", 0.001)

    class FailingRecoveryHost(_FakeHost):
        def __init__(self) -> None:
            super().__init__()
            self.lanes = {"WP01": "in_review"}
            self.last_actors = {"WP01": self.actor}

        def transition(self, mission, wp, to, **kwargs):
            raise RuntimeError("host recovery rejected")

    host = FailingRecoveryHost()
    cfg = _cfg(tmp_path)
    run_state = new_run_state("m", _policy())

    with pytest.raises(loop_mod.OrchestrationError, match="host recovery rejected"):
        asyncio.run(
            run_orchestration_loop(
                "m", host, run_state, cfg, recover_in_review=True
            )
        )


def test_already_claimed_wp_quarantines_no_infinite_loop(tmp_path, monkeypatch) -> None:
    """A WP claimed by another actor should surface as stalled, not poll forever."""
    monkeypatch.setattr(loop_mod, "LOOP_POLL_INTERVAL", 0.001)
    monkeypatch.setattr(loop_mod, "DEADLOCK_THRESHOLD", 2)

    class ClaimedHost(_FakeHost):
        def start_implementation(self, mission, wp):
            self.start_impl_calls.append(wp)
            raise WPAlreadyClaimedError("WP_ALREADY_CLAIMED", "claimed by other-actor")

    host = ClaimedHost()
    cfg = _cfg(tmp_path)
    run_state = new_run_state("m", _policy())

    async def run():
        await asyncio.wait_for(
            run_orchestration_loop("m", host, run_state, cfg), timeout=5.0
        )

    with pytest.raises(DeadlockError) as exc:
        asyncio.run(run())

    assert host.start_impl_calls == ["WP01"]
    assert "claimed by other-actor" in str(exc.value)
