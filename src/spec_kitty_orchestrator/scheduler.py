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
    "select_implementer",
    "select_reviewer",
]
