"""Failure classification, retry, and fallback logic.

Classifies execution failures and decides whether to retry with the same agent,
fallback to a different agent, or escalate to human intervention.
All lane transitions go through HostClient — never direct file writes.
"""

from __future__ import annotations

import logging
from enum import Enum

from .agents.base import InvocationResult
from .executor import TIMEOUT_EXIT_CODE

logger = logging.getLogger(__name__)

RETRY_DELAY_SECONDS = 5
MAX_ERROR_LENGTH = 500


class FailureType(str, Enum):
    """Classification of execution failures for retry/fallback decisions."""

    TIMEOUT = "timeout"
    AUTH_ERROR = "auth_error"
    RATE_LIMIT = "rate_limit"
    GENERAL_ERROR = "general_error"
    NETWORK_ERROR = "network_error"


def is_success(result: InvocationResult) -> bool:
    """Return True if the invocation succeeded (exit 0 and success flag)."""
    return result.exit_code == 0 and result.success


def classify_failure(result: InvocationResult, agent_id: str) -> FailureType:
    """Classify the type of failure to guide retry/fallback strategy.

    Args:
        result: The failed invocation result.
        agent_id: The agent that produced this result.

    Returns:
        FailureType enum value.
    """
    if result.exit_code == TIMEOUT_EXIT_CODE:
        return FailureType.TIMEOUT

    # Auth errors: Gemini exit 41, or "auth" in error messages
    if result.exit_code == 41 or any(
        "auth" in e.lower() for e in result.errors
    ):
        return FailureType.AUTH_ERROR

    # Rate limit: Gemini exit 42, or rate-limit in messages
    if result.exit_code == 42 or any(
        "rate" in e.lower() and "limit" in e.lower() for e in result.errors
    ):
        return FailureType.RATE_LIMIT

    # Network errors
    if any(
        kw in e.lower()
        for e in result.errors
        for kw in ("network", "connection", "timeout", "unreachable")
    ):
        return FailureType.NETWORK_ERROR

    return FailureType.GENERAL_ERROR


def should_retry(
    failure_type: FailureType,
    retries_so_far: int,
    max_retries: int,
) -> bool:
    """Return True if the WP should be retried with the same agent.

    Auth errors are never retried (credentials won't change during a run).
    Timeouts and rate limits exhaust retries then fall back.

    Args:
        failure_type: Classified failure type.
        retries_so_far: How many times this agent has been tried.
        max_retries: Maximum retries allowed by config.

    Returns:
        True if a retry should be attempted.
    """
    if failure_type == FailureType.AUTH_ERROR:
        return False
    return retries_so_far < max_retries


def should_fallback(
    failure_type: FailureType,
    retries_exhausted: bool,
    has_fallback_agent: bool,
) -> bool:
    """Return True if we should try a fallback agent.

    Args:
        failure_type: Classified failure type.
        retries_exhausted: True if retry limit reached.
        has_fallback_agent: True if another agent is available.

    Returns:
        True if fallback should be attempted.
    """
    if not has_fallback_agent:
        return False
    if failure_type == FailureType.AUTH_ERROR:
        return True  # Immediately fallback on auth errors
    return retries_exhausted


def truncate_error(error: str) -> str:
    """Truncate error message to MAX_ERROR_LENGTH characters."""
    if len(error) <= MAX_ERROR_LENGTH:
        return error
    return error[:MAX_ERROR_LENGTH] + "..."


def extract_review_feedback(result: InvocationResult) -> str | None:
    """Extract actionable review feedback from the result.

    Looks for structured feedback in JSON output or falls back to stdout.

    Args:
        result: The review InvocationResult.

    Returns:
        Feedback string or None if not extractable.
    """
    if result.errors:
        return "\n".join(result.errors[:3])
    if result.stdout.strip():
        # Return the last ~500 chars of stdout as feedback
        tail = result.stdout.strip()[-500:]
        return tail
    return None


__all__ = [
    "FailureType",
    "is_success",
    "classify_failure",
    "should_retry",
    "should_fallback",
    "truncate_error",
    "extract_review_feedback",
    "RETRY_DELAY_SECONDS",
]
