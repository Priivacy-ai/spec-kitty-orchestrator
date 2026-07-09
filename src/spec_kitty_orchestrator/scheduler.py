"""Concurrency manager and agent selection for the orchestration loop.

Manages WP slot availability and enforces max_concurrent_wps limit.
Agent selection logic delegates to AgentSelectionConfig.
"""

from __future__ import annotations

import asyncio
import logging

from .config import AgentSelectionConfig

logger = logging.getLogger(__name__)


class SchedulerError(Exception):
    """Base exception for scheduler errors."""


class NoAgentAvailableError(SchedulerError):
    """Raised when no agent is available for the requested role."""


# Lanes whose WPs the orchestrator actively drives through execute_and_advance.
# A WP left in one of these by a prior/interrupted run (or an out-of-band
# start-implementation, e.g. during host-side testing) is "orphaned": list-ready
# never returns a claimed/in_progress/for_review WP, so without adoption the loop
# can never make progress on it and falsely reports a dependency deadlock.
#
# ``for_review`` is resumable too: a WP parked in for_review by an interrupted run
# needs a REVIEWER dispatched on resume (not an implementer). The loop routes an
# adopted for_review WP straight to the review phase (execute_and_advance with
# current_lane="for_review"), so review->approve->done proceeds and dependent
# planned WPs unblock. claimed/in_progress are re-implemented; for_review is
# reviewed.
RESUMABLE_LANES = frozenset({"claimed", "in_progress", "for_review"})


def select_schedulable_wp_ids(
    ready_wps: list,
    state_wps: list,
    active_ids: set[str],
    driven_ids: set[str],
) -> list[str]:
    """Return the WP IDs the loop should schedule this iteration.

    Combines two sources:
    - ``ready_wps`` (from list-ready): planned WPs whose dependencies are met.
    - ``state_wps`` (from mission-state): WPs orphaned in a RESUMABLE lane by a
      prior/interrupted run, which list-ready never surfaces. Adopting these is
      what lets an interrupted mission resume instead of dead-locking.

    Excludes WPs already active in this process (``active_ids``), and WPs this
    process has already driven (``driven_ids``) so a WP whose allocation or run
    failed is not re-adopted into an infinite loop — it is surfaced as stalled
    instead.

    Order: ready WPs first (in list-ready order), then orphaned resumables;
    de-duplicated.
    """
    ordered: list[str] = []
    seen: set[str] = set()
    for wp in ready_wps:
        if (
            wp.wp_id not in seen
            and wp.wp_id not in active_ids
            and wp.wp_id not in driven_ids
        ):
            ordered.append(wp.wp_id)
            seen.add(wp.wp_id)
    for wp in state_wps:
        if (
            wp.lane in RESUMABLE_LANES
            and wp.wp_id not in seen
            and wp.wp_id not in active_ids
            and wp.wp_id not in driven_ids
        ):
            ordered.append(wp.wp_id)
            seen.add(wp.wp_id)
    return ordered


class ConcurrencyManager:
    """Semaphore-based concurrency limiter for in-flight WPs.

    Tracks which WPs are currently executing so the loop can avoid
    double-scheduling.
    """

    def __init__(self, max_concurrent: int) -> None:
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._active: set[str] = set()

    def has_slot(self) -> bool:
        """Return True if a concurrency slot is available."""
        return self._semaphore._value > 0  # type: ignore[attr-defined]

    def is_active(self, wp_id: str) -> bool:
        """Return True if this WP is currently being executed."""
        return wp_id in self._active

    def mark_active(self, wp_id: str) -> None:
        """Mark a WP as currently executing (non-blocking)."""
        self._active.add(wp_id)

    def mark_idle(self, wp_id: str) -> None:
        """Mark a WP as no longer executing."""
        self._active.discard(wp_id)

    def active_count(self) -> int:
        """Return number of WPs currently executing."""
        return len(self._active)

    def active_wp_ids(self) -> set[str]:
        """Return the set of currently active WP IDs."""
        return set(self._active)

    async def acquire(self) -> None:
        """Acquire a concurrency slot (blocks if all slots used)."""
        await self._semaphore.acquire()

    def release(self) -> None:
        """Release a concurrency slot."""
        self._semaphore.release()


def select_implementer(
    agent_cfg: AgentSelectionConfig,
    fallback_agents_tried: list[str],
) -> str:
    """Select next implementation agent.

    Args:
        agent_cfg: Agent selection configuration.
        fallback_agents_tried: Agents already attempted for this WP.

    Returns:
        Agent ID to use.

    Raises:
        NoAgentAvailableError: If all agents have been tried.
    """
    agent_id = agent_cfg.select_implementer(tried=fallback_agents_tried)
    if agent_id is None:
        raise NoAgentAvailableError(
            f"All implementation agents exhausted: {fallback_agents_tried}"
        )
    return agent_id


def select_reviewer(
    agent_cfg: AgentSelectionConfig,
    impl_agent: str | None,
    fallback_agents_tried: list[str],
) -> str:
    """Select next review agent.

    Args:
        agent_cfg: Agent selection configuration.
        impl_agent: Implementation agent (for single-agent mode).
        fallback_agents_tried: Review agents already attempted.

    Returns:
        Agent ID to use for review.

    Raises:
        NoAgentAvailableError: If all review agents have been tried.
    """
    agent_id = agent_cfg.select_reviewer(
        impl_agent=impl_agent, tried=fallback_agents_tried
    )
    if agent_id is None:
        raise NoAgentAvailableError(
            f"All review agents exhausted: {fallback_agents_tried}"
        )
    return agent_id


__all__ = [
    "ConcurrencyManager",
    "SchedulerError",
    "NoAgentAvailableError",
    "RESUMABLE_LANES",
    "select_implementer",
    "select_reviewer",
    "select_schedulable_wp_ids",
]
