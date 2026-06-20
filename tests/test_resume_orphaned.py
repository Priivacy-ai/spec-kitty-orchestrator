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
from spec_kitty_orchestrator.loop import DeadlockError, run_orchestration_loop
from spec_kitty_orchestrator.policy import PolicyMetadata
from spec_kitty_orchestrator.scheduler import select_schedulable_wp_ids
from spec_kitty_orchestrator.state import new_run_state


def _ready(wp_id: str):
    return SimpleNamespace(wp_id=wp_id)


def _state(wp_id: str, lane: str):
    return SimpleNamespace(wp_id=wp_id, lane=lane, dependencies=[], last_actor=None)


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

    def test_non_resumable_lanes_are_ignored(self) -> None:
        wps = [_state("WP01", "planned"), _state("WP02", "for_review"), _state("WP03", "done")]
        assert select_schedulable_wp_ids([], wps, set(), set()) == []

    def test_dedup_ready_and_state(self) -> None:
        ids = select_schedulable_wp_ids([_ready("WP01")], [_state("WP01", "in_progress")], set(), set())
        assert ids == ["WP01"]


# -- loop-level regression ----------------------------------------------------


class _FakeHost:
    """Minimal HostClient stand-in. WP01 starts orphaned in_progress."""

    def __init__(self) -> None:
        self.lanes = {"WP01": "in_progress"}
        self.start_impl_calls: list[str] = []

    def list_ready(self, mission):  # in_progress WP is never "ready"
        return SimpleNamespace(ready_work_packages=[])

    def mission_state(self, mission):
        return SimpleNamespace(
            work_packages=[_state(k, v) for k, v in self.lanes.items()]
        )

    def start_implementation(self, mission, wp):
        self.start_impl_calls.append(wp)
        return SimpleNamespace(workspace_path="/tmp/ws", prompt_path="/tmp/p.md")


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

    async def fake_execute_and_advance(wp_id, mission, ws, pp, agent, h, rs, ac, cfg, conc):
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
            # WP01 stuck in for_review (not resumable by this orchestrator).
            self.lanes = {"WP01": "for_review"}

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
    assert "for_review" in str(exc.value)
    assert host.start_impl_calls == [], "for_review WP must not be (re)started"
