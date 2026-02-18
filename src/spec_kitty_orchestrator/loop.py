"""Main async orchestration loop.

Polls host for ready WPs, assigns agents, executes impl → review → done cycles.
All host state transitions go through HostClient. Provider-local state is
persisted via save_state after each significant event.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path

from .agents import get_invoker
from .config import AgentSelectionConfig, OrchestratorConfig
from .executor import TIMEOUT_EXIT_CODE, execute_agent, get_log_path
from .host.client import HostClient, TransitionRejectedError, WPAlreadyClaimedError
from .monitor import (
    classify_failure,
    extract_review_feedback,
    is_success,
    should_fallback,
    should_retry,
    truncate_error,
)
from .scheduler import ConcurrencyManager, NoAgentAvailableError, select_implementer, select_reviewer
from .state import RunState, WPExecution, save_state

logger = logging.getLogger(__name__)

LOOP_POLL_INTERVAL = 2.0  # seconds between list-ready polls
DEADLOCK_THRESHOLD = 3  # consecutive empty-ready polls before declaring deadlock


class OrchestrationError(Exception):
    """Fatal orchestration error."""


class DeadlockError(OrchestrationError):
    """Raised when the loop detects a dependency deadlock."""


async def execute_and_advance(
    wp_id: str,
    feature: str,
    workspace_path: Path,
    prompt_path: Path,
    impl_agent_id: str,
    host: HostClient,
    run_state: RunState,
    agent_cfg: AgentSelectionConfig,
    cfg: OrchestratorConfig,
    concurrency: ConcurrencyManager,
) -> None:
    """Execute one WP through the full impl → (review →)* done lifecycle.

    Handles retries and fallback for both implementation and review phases.
    Releases the concurrency slot when done (success or exhausted).

    Args:
        wp_id: Work package ID.
        feature: Feature slug.
        workspace_path: Worktree path returned by host.start_implementation.
        prompt_path: WP markdown prompt file path.
        impl_agent_id: Selected implementation agent ID.
        host: HostClient for all state mutations.
        run_state: Provider-local run state (mutated in-place).
        agent_cfg: Agent selection config.
        cfg: Full orchestrator config.
        concurrency: Concurrency manager (already acquired before this call).
    """
    wp_exec = run_state.get_or_create_wp(wp_id)
    wp_exec.implementation_agent = impl_agent_id
    save_state(run_state, cfg.state_file)

    try:
        prompt_text = prompt_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.error("Cannot read prompt %s: %s", prompt_path, exc)
        _mark_failed(wp_exec, str(exc))
        save_state(run_state, cfg.state_file)
        return

    # ── Implementation phase ──────────────────────────────────────────────

    impl_success = False
    while not impl_success:
        invoker = get_invoker(impl_agent_id)
        log_file = get_log_path(cfg.log_dir, feature, wp_id, "implementation")
        wp_exec.log_file = str(log_file)
        save_state(run_state, cfg.state_file)

        host.append_history(
            feature, wp_id,
            f"Starting implementation with agent '{impl_agent_id}' (retry #{wp_exec.implementation_retries})"
        )

        result = await execute_agent(
            invoker, prompt_text, workspace_path,
            role="implementation",
            timeout_seconds=agent_cfg.timeout_seconds,
            log_file=log_file,
        )

        if is_success(result):
            impl_success = True
            host.append_history(
                feature, wp_id,
                f"Implementation completed successfully by '{impl_agent_id}'"
            )
            break

        # Implementation failed
        failure = classify_failure(result, impl_agent_id)
        error_msg = truncate_error("; ".join(result.errors) if result.errors else "unknown error")
        wp_exec.last_error = error_msg
        logger.warning("WP %s impl failed (%s): %s", wp_id, failure.value, error_msg)

        if should_retry(failure, wp_exec.implementation_retries, agent_cfg.max_retries):
            wp_exec.implementation_retries += 1
            save_state(run_state, cfg.state_file)
            host.append_history(feature, wp_id, f"Retrying implementation (attempt {wp_exec.implementation_retries})")
            await asyncio.sleep(2.0 * wp_exec.implementation_retries)
            continue

        # Try fallback agent
        wp_exec.fallback_agents_tried.append(impl_agent_id)
        try:
            impl_agent_id = select_implementer(agent_cfg, wp_exec.fallback_agents_tried)
            wp_exec.implementation_agent = impl_agent_id
            wp_exec.implementation_retries = 0
            save_state(run_state, cfg.state_file)
            host.append_history(feature, wp_id, f"Falling back to agent '{impl_agent_id}'")
        except NoAgentAvailableError:
            logger.error("WP %s: all implementation agents exhausted", wp_id)
            host.append_history(feature, wp_id, "FAILED: all implementation agents exhausted")
            try:
                host.transition(feature, wp_id, "blocked", note="All implementation agents exhausted")
            except Exception:
                pass
            _mark_failed(wp_exec, "All implementation agents exhausted")
            save_state(run_state, cfg.state_file)
            return

    # Transition to for_review
    try:
        host.transition(feature, wp_id, "for_review", note=f"Implementation by '{impl_agent_id}' complete")
    except TransitionRejectedError as exc:
        logger.warning("WP %s: for_review transition rejected: %s", wp_id, exc)
        _mark_failed(wp_exec, str(exc))
        save_state(run_state, cfg.state_file)
        return

    # ── Review phase ──────────────────────────────────────────────────────
    # WP is in for_review. The reviewer runs while the WP *stays* in for_review.
    # Allowed transitions from for_review:
    #   for_review → done          (reviewer approved)
    #   for_review → in_progress   (reviewer rejected; via start_review)
    # in_progress → done is NOT allowed, so start_review must NOT be called
    # before running the review agent.

    review_agent_id = select_reviewer(agent_cfg, impl_agent_id, [])
    review_cycle = 0
    review_done = False

    while not review_done:
        review_cycle += 1

        wp_exec.review_agent = review_agent_id
        review_log = get_log_path(cfg.log_dir, feature, wp_id, f"review-{review_cycle}")
        save_state(run_state, cfg.state_file)

        host.append_history(
            feature, wp_id,
            f"Starting review cycle {review_cycle} with '{review_agent_id}'"
        )

        # Run review while WP remains in for_review
        review_result = await execute_agent(
            get_invoker(review_agent_id),
            prompt_text,
            workspace_path,
            role="review",
            timeout_seconds=agent_cfg.timeout_seconds,
            log_file=review_log,
        )

        if is_success(review_result):
            # Approved: for_review → done  (this transition IS allowed)
            review_ref = f"review-{wp_id}-cycle{review_cycle}-{uuid.uuid4().hex[:8]}"
            try:
                host.transition(
                    feature, wp_id, "done",
                    note=f"Review approved by '{review_agent_id}'",
                    review_ref=review_ref,
                )
                review_done = True
                host.append_history(feature, wp_id, f"Review approved in cycle {review_cycle}")
                logger.info("WP %s completed successfully", wp_id)
            except TransitionRejectedError as exc:
                logger.error("WP %s: done transition rejected: %s", wp_id, exc)
            break

        # Rejected — extract feedback, enforce retry limit
        feedback = extract_review_feedback(review_result)
        wp_exec.review_feedback = feedback
        wp_exec.review_retries += 1
        save_state(run_state, cfg.state_file)

        if wp_exec.review_retries > agent_cfg.max_retries:
            logger.error("WP %s: review retry limit exceeded", wp_id)
            host.append_history(feature, wp_id, "FAILED: review retry limit exceeded")
            try:
                host.transition(feature, wp_id, "blocked", note="Review cycle limit exceeded")
            except Exception:
                pass
            break

        feedback_ref = f"feedback-{wp_id}-cycle{review_cycle}-{uuid.uuid4().hex[:8]}"
        host.append_history(
            feature, wp_id,
            f"Review cycle {review_cycle} rejected. Feedback: {(feedback or 'none')[:200]}"
        )

        # for_review → in_progress via start_review (the right use of start_review:
        # triggering a re-implementation cycle after rejection)
        try:
            host.start_review(feature, wp_id, review_ref=feedback_ref)
        except TransitionRejectedError as exc:
            logger.error("WP %s: start-review (re-impl trigger) rejected: %s", wp_id, exc)
            break

        # Run re-implementation with review feedback
        reimpl_log = get_log_path(cfg.log_dir, feature, wp_id, f"reimpl-{review_cycle}")
        reimpl_prompt = _build_rework_prompt(prompt_text, feedback)
        reimpl_result = await execute_agent(
            get_invoker(impl_agent_id),
            reimpl_prompt,
            workspace_path,
            role="implementation",
            timeout_seconds=agent_cfg.timeout_seconds,
            log_file=reimpl_log,
        )
        if not is_success(reimpl_result):
            error_msg = truncate_error(
                "; ".join(reimpl_result.errors) if reimpl_result.errors else "rework failed"
            )
            host.append_history(feature, wp_id, f"Re-implementation failed: {error_msg}")
            try:
                host.transition(feature, wp_id, "blocked", note=f"Re-implementation failed: {error_msg}")
            except Exception:
                pass
            break

        # in_progress → for_review (back to review queue for next cycle)
        try:
            host.transition(
                feature, wp_id, "for_review",
                note=f"Re-implementation complete (cycle {review_cycle})"
            )
        except TransitionRejectedError as exc:
            logger.error("WP %s: for_review re-transition rejected: %s", wp_id, exc)
            break

    save_state(run_state, cfg.state_file)


def _build_rework_prompt(original_prompt: str, feedback: str | None) -> str:
    """Build a rework prompt incorporating review feedback."""
    if not feedback:
        return original_prompt
    return (
        f"{original_prompt}\n\n"
        f"## Review Feedback (address before resubmitting)\n\n"
        f"{feedback}\n"
    )


def _mark_failed(wp_exec: WPExecution, error: str) -> None:
    """Record failure in WPExecution."""
    wp_exec.last_error = error[:500]


async def run_orchestration_loop(
    feature: str,
    host: HostClient,
    run_state: RunState,
    cfg: OrchestratorConfig,
) -> None:
    """Main async orchestration loop.

    Continuously polls for ready WPs, dispatches them to agents, and waits
    until all WPs are done or a deadlock is detected.

    Args:
        feature: Feature slug.
        host: HostClient for all host interactions.
        run_state: Provider-local run state.
        cfg: Full orchestrator config.
    """
    concurrency = ConcurrencyManager(cfg.max_concurrent_wps)
    agent_cfg = cfg.agent_selection
    active_tasks: set[asyncio.Task] = set()  # type: ignore[type-arg]
    empty_ready_streak = 0

    logger.info("Orchestration loop started for feature '%s'", feature)

    while True:
        ready_data = host.list_ready(feature)
        ready_wps = ready_data.ready_work_packages

        # Filter out already-active WPs
        schedulable = [
            wp for wp in ready_wps
            if not concurrency.is_active(wp.wp_id)
        ]

        if not schedulable and concurrency.active_count() == 0:
            # Check if all WPs are done
            state_data = host.feature_state(feature)
            all_lanes = [wp.lane for wp in state_data.work_packages]
            terminal_lanes = {"done", "canceled", "blocked"}
            if all(lane in terminal_lanes for lane in all_lanes if lane):
                logger.info("All WPs reached terminal state. Orchestration complete.")
                break

            empty_ready_streak += 1
            if empty_ready_streak >= DEADLOCK_THRESHOLD:
                non_terminal = [
                    wp.wp_id for wp in state_data.work_packages
                    if wp.lane not in terminal_lanes
                ]
                raise DeadlockError(
                    f"Dependency deadlock detected. Non-terminal WPs: {non_terminal}"
                )
        else:
            empty_ready_streak = 0

        # Schedule ready WPs
        for wp in schedulable:
            if not concurrency.has_slot():
                break

            try:
                impl_agent_id = select_implementer(
                    agent_cfg,
                    run_state.get_or_create_wp(wp.wp_id).fallback_agents_tried,
                )
            except NoAgentAvailableError:
                logger.warning("WP %s: no implementation agent available, skipping", wp.wp_id)
                continue

            # Claim the WP via host
            try:
                impl_resp = host.start_implementation(feature, wp.wp_id)
            except WPAlreadyClaimedError:
                logger.debug("WP %s already claimed, skipping", wp.wp_id)
                continue
            except Exception as exc:
                logger.error("WP %s: start-implementation failed: %s", wp.wp_id, exc)
                continue

            workspace_path = Path(impl_resp.workspace_path)
            prompt_path = Path(impl_resp.prompt_path)

            concurrency.mark_active(wp.wp_id)
            await concurrency.acquire()

            task = asyncio.create_task(
                _run_wp_task(
                    wp.wp_id, feature, workspace_path, prompt_path,
                    impl_agent_id, host, run_state, agent_cfg, cfg, concurrency,
                )
            )
            active_tasks.add(task)
            task.add_done_callback(active_tasks.discard)

        # Clean up completed tasks
        done_tasks = {t for t in active_tasks if t.done()}
        for t in done_tasks:
            active_tasks.discard(t)
            exc = t.exception()
            if exc:
                logger.error("WP task raised exception: %s", exc)

        await asyncio.sleep(LOOP_POLL_INTERVAL)

    # Wait for all in-flight tasks
    if active_tasks:
        await asyncio.gather(*active_tasks, return_exceptions=True)

    save_state(run_state, cfg.state_file)
    logger.info("Orchestration loop completed for feature '%s'", feature)


async def _run_wp_task(
    wp_id: str,
    feature: str,
    workspace_path: Path,
    prompt_path: Path,
    impl_agent_id: str,
    host: HostClient,
    run_state: RunState,
    agent_cfg: AgentSelectionConfig,
    cfg: OrchestratorConfig,
    concurrency: ConcurrencyManager,
) -> None:
    """Wrapper that releases concurrency slot after execute_and_advance."""
    try:
        await execute_and_advance(
            wp_id, feature, workspace_path, prompt_path,
            impl_agent_id, host, run_state, agent_cfg, cfg, concurrency,
        )
    finally:
        concurrency.mark_idle(wp_id)
        concurrency.release()


__all__ = [
    "run_orchestration_loop",
    "execute_and_advance",
    "OrchestrationError",
    "DeadlockError",
]
