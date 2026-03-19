"""Main async orchestration loop.

Polls host for ready WPs, assigns agents, executes impl → review → done cycles.
All host state transitions go through HostClient. Provider-local state is
persisted via save_state after each significant event.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .agents import get_invoker
from .config import AgentSelectionConfig, OrchestratorConfig
from .executor import TIMEOUT_EXIT_CODE, execute_agent, get_log_path
from .host.client import HostClient, TaskWorkflowError, TransitionRejectedError, WPAlreadyClaimedError
from .monitor import (
    FailureType,
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
EXECUTION_HEARTBEAT_SECONDS = 5.0


class OrchestrationError(Exception):
    """Fatal orchestration error."""


class DeadlockError(OrchestrationError):
    """Raised when the loop detects a dependency deadlock."""


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_wp_paths(repo_root: Path, feature: str, wp_id: str) -> tuple[Path, Path]:
    """Resolve the canonical workspace and prompt paths for a WP."""
    workspace_path = repo_root / ".worktrees" / f"{feature}-{wp_id}"
    task_dir = repo_root / "kitty-specs" / feature / "tasks"
    prompt_matches = sorted(task_dir.glob(f"{wp_id}*.md"))
    if not prompt_matches:
        raise OrchestrationError(
            f"Cannot resolve prompt path for {feature}/{wp_id}: no matching task file found"
        )
    return workspace_path, prompt_matches[0]


def _select_orphaned_recovery_mode(wp_exec: WPExecution) -> str:
    """Pick a recovery mode for a WP stuck in `in_progress`."""
    if (
        wp_exec.review_started_at
        or wp_exec.review_agent
        or wp_exec.review_retries > 0
        or wp_exec.review_feedback
    ):
        return "review"
    return "implementation"


def _extract_prompt_subtasks(prompt_text: str) -> list[str]:
    """Parse WP frontmatter and return declared subtasks."""
    lines = prompt_text.splitlines()
    if not lines or lines[0].strip() != "---":
        return []

    subtasks: list[str] = []
    in_subtasks = False
    for line in lines[1:]:
        stripped = line.strip()
        if stripped == "---":
            break
        if not in_subtasks and stripped == "subtasks:":
            in_subtasks = True
            continue
        if in_subtasks:
            if stripped.startswith("- "):
                task_id = stripped[2:].strip()
                if task_id:
                    subtasks.append(task_id)
                continue
            if stripped and not line.startswith((" ", "\t", "-")):
                in_subtasks = False
    return subtasks


async def _execute_with_heartbeat(
    *,
    invoker,
    prompt_text: str,
    workspace_path: Path,
    role: str,
    timeout_seconds: int,
    log_file: Path,
    wp_exec: WPExecution,
    run_state: RunState,
    cfg: OrchestratorConfig,
):
    """Run an agent while updating provider-local heartbeat state."""
    started_at_field = f"{role}_started_at"
    completed_at_field = f"{role}_completed_at"
    heartbeat_field = f"{role}_heartbeat_at"

    now = _now_utc()
    if getattr(wp_exec, started_at_field) is None:
        setattr(wp_exec, started_at_field, now)
    setattr(wp_exec, heartbeat_field, now)
    save_state(run_state, cfg.state_file)

    task = asyncio.create_task(
        execute_agent(
            invoker,
            prompt_text,
            workspace_path,
            role=role,
            timeout_seconds=timeout_seconds,
            log_file=log_file,
        )
    )

    while not task.done():
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=EXECUTION_HEARTBEAT_SECONDS)
        except asyncio.TimeoutError:
            setattr(wp_exec, heartbeat_field, _now_utc())
            save_state(run_state, cfg.state_file)

    result = await task
    now = _now_utc()
    setattr(wp_exec, completed_at_field, now)
    setattr(wp_exec, heartbeat_field, now)
    save_state(run_state, cfg.state_file)
    return result


def _reconcile_wp_subtasks(
    host: HostClient,
    feature: str,
    wp_id: str,
    prompt_text: str,
) -> list[str]:
    """Mark prompt-declared subtasks done after a successful implementation."""
    subtasks = _extract_prompt_subtasks(prompt_text)
    if not subtasks:
        return []

    host.mark_task_status(feature, subtasks, "done")
    host.append_history(
        feature,
        wp_id,
        f"Reconciled subtasks as done after successful implementation: {', '.join(subtasks)}",
    )
    return subtasks


async def _run_review_phase(
    wp_id: str,
    feature: str,
    workspace_path: Path,
    prompt_path: Path,
    impl_agent_id: str,
    host: HostClient,
    run_state: RunState,
    agent_cfg: AgentSelectionConfig,
    cfg: OrchestratorConfig,
) -> None:
    """Execute review and any follow-up rework cycles for an already-implemented WP."""
    wp_exec = run_state.get_or_create_wp(wp_id)

    try:
        prompt_text = prompt_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.error("Cannot read prompt %s: %s", prompt_path, exc)
        _mark_failed(wp_exec, str(exc))
        save_state(run_state, cfg.state_file)
        return

    review_agent_id = select_reviewer(agent_cfg, impl_agent_id, [])
    review_cycle = wp_exec.review_retries
    review_done = False
    review_agents_tried: list[str] = []

    while not review_done:
        review_cycle += 1
        wp_exec.review_agent = review_agent_id
        review_log = get_log_path(cfg.log_dir, feature, wp_id, f"review-{review_cycle}")
        wp_exec.log_file = str(review_log)
        save_state(run_state, cfg.state_file)

        host.append_history(
            feature, wp_id,
            f"Starting review cycle {review_cycle} with '{review_agent_id}'"
        )

        review_result = await _execute_with_heartbeat(
            invoker=get_invoker(review_agent_id),
            prompt_text=prompt_text,
            workspace_path=workspace_path,
            role="review",
            timeout_seconds=agent_cfg.timeout_seconds,
            log_file=review_log,
            wp_exec=wp_exec,
            run_state=run_state,
            cfg=cfg,
        )

        if is_success(review_result):
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

        failure = classify_failure(review_result, review_agent_id)
        if failure in {
            FailureType.TIMEOUT,
            FailureType.AUTH_ERROR,
            FailureType.RATE_LIMIT,
            FailureType.NETWORK_ERROR,
        }:
            error_msg = truncate_error(
                "; ".join(review_result.errors) if review_result.errors else f"Review execution failed: {failure}"
            )
            wp_exec.last_error = error_msg
            logger.warning("WP %s review execution failed (%s): %s", wp_id, failure, error_msg)

            if should_retry(failure, wp_exec.review_retries, agent_cfg.max_retries):
                wp_exec.review_retries += 1
                save_state(run_state, cfg.state_file)
                host.append_history(
                    feature,
                    wp_id,
                    f"Retrying review after execution failure ({failure}): {error_msg}",
                )
                await asyncio.sleep(2.0 * wp_exec.review_retries)
                continue

            review_agents_tried.append(review_agent_id)
            try:
                review_agent_id = select_reviewer(agent_cfg, impl_agent_id, review_agents_tried)
                wp_exec.review_agent = review_agent_id
                wp_exec.review_retries = 0
                save_state(run_state, cfg.state_file)
                host.append_history(
                    feature,
                    wp_id,
                    f"Falling back review to agent '{review_agent_id}' after execution failure ({failure})",
                )
                continue
            except NoAgentAvailableError:
                logger.error("WP %s: all review agents exhausted", wp_id)
                host.append_history(feature, wp_id, "FAILED: all review agents exhausted")
                try:
                    host.transition(feature, wp_id, "blocked", note="All review agents exhausted")
                except Exception:
                    pass
                break

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

        try:
            host.start_review(feature, wp_id, review_ref=feedback_ref)
        except TransitionRejectedError as exc:
            logger.error("WP %s: start-review (re-impl trigger) rejected: %s", wp_id, exc)
            break

        reimpl_log = get_log_path(cfg.log_dir, feature, wp_id, f"reimpl-{review_cycle}")
        reimpl_prompt = _build_rework_prompt(prompt_text, feedback)
        reimpl_result = await _execute_with_heartbeat(
            invoker=get_invoker(impl_agent_id),
            prompt_text=reimpl_prompt,
            workspace_path=workspace_path,
            role="implementation",
            timeout_seconds=agent_cfg.timeout_seconds,
            log_file=reimpl_log,
            wp_exec=wp_exec,
            run_state=run_state,
            cfg=cfg,
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

        try:
            _reconcile_wp_subtasks(host, feature, wp_id, prompt_text)
            host.move_task_for_review(
                feature,
                wp_id,
                impl_agent_id,
                note=f"Re-implementation complete (cycle {review_cycle})",
            )
        except (TransitionRejectedError, TaskWorkflowError) as exc:
            error_msg = truncate_error(str(exc))
            logger.error("WP %s: for_review re-transition rejected: %s", wp_id, exc)
            host.append_history(
                feature,
                wp_id,
                f"FAILED: re-implementation handoff rejected: {error_msg}",
            )
            try:
                host.transition(
                    feature,
                    wp_id,
                    "blocked",
                    note=f"Re-implementation handoff rejected: {error_msg}",
                )
            except Exception:
                pass
            wp_exec.last_error = error_msg
            save_state(run_state, cfg.state_file)
            break

    save_state(run_state, cfg.state_file)


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

        result = await _execute_with_heartbeat(
            invoker=invoker,
            prompt_text=prompt_text,
            workspace_path=workspace_path,
            role="implementation",
            timeout_seconds=agent_cfg.timeout_seconds,
            log_file=log_file,
            wp_exec=wp_exec,
            run_state=run_state,
            cfg=cfg,
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

    # Transition to for_review via the task workflow handoff so WP checklist
    # readiness is validated before review starts.
    try:
        _reconcile_wp_subtasks(host, feature, wp_id, prompt_text)
        host.move_task_for_review(
            feature,
            wp_id,
            impl_agent_id,
            note=f"Implementation by '{impl_agent_id}' complete",
        )
    except (TransitionRejectedError, TaskWorkflowError) as exc:
        error_msg = truncate_error(str(exc))
        logger.warning("WP %s: for_review transition rejected: %s", wp_id, exc)
        host.append_history(feature, wp_id, f"FAILED: implementation handoff rejected: {error_msg}")
        try:
            host.transition(
                feature,
                wp_id,
                "blocked",
                note=f"Implementation handoff rejected: {error_msg}",
            )
        except Exception:
            pass
        _mark_failed(wp_exec, str(exc))
        save_state(run_state, cfg.state_file)
        return

    await _run_review_phase(
        wp_id=wp_id,
        feature=feature,
        workspace_path=workspace_path,
        prompt_path=prompt_path,
        impl_agent_id=impl_agent_id,
        host=host,
        run_state=run_state,
        agent_cfg=agent_cfg,
        cfg=cfg,
    )


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
        state_data = host.feature_state(feature)

        # Filter out already-active WPs
        schedulable = [
            wp for wp in ready_wps
            if not concurrency.is_active(wp.wp_id)
        ]
        recovery_candidates = [
            wp for wp in state_data.work_packages
            if wp.lane in {"in_progress", "for_review"} and not concurrency.is_active(wp.wp_id)
        ]

        if not schedulable and not recovery_candidates and concurrency.active_count() == 0:
            # Check if all WPs are done
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

        # Resume any orphaned in-flight WPs before claiming new planned work.
        for wp in recovery_candidates:
            if not concurrency.has_slot():
                break

            try:
                workspace_path, prompt_path = _resolve_wp_paths(cfg.repo_root, feature, wp.wp_id)
            except OrchestrationError as exc:
                logger.error("WP %s: cannot resolve recovery paths: %s", wp.wp_id, exc)
                continue

            wp_exec = run_state.get_or_create_wp(wp.wp_id)
            impl_agent_id = wp_exec.implementation_agent
            if impl_agent_id is None:
                try:
                    impl_agent_id = select_implementer(agent_cfg, wp_exec.fallback_agents_tried)
                except NoAgentAvailableError:
                    logger.warning("WP %s: no implementation agent available for recovery", wp.wp_id)
                    continue

            if wp.lane == "for_review":
                logger.info("WP %s: resuming orphaned review cycle", wp.wp_id)
                task_factory = _run_review_task
            elif _select_orphaned_recovery_mode(wp_exec) == "review":
                logger.info("WP %s: resuming orphaned in-progress review cycle", wp.wp_id)
                task_factory = _run_review_task
            else:
                logger.info("WP %s: resuming orphaned implementation cycle", wp.wp_id)
                task_factory = _run_wp_task

            concurrency.mark_active(wp.wp_id)
            await concurrency.acquire()

            task = asyncio.create_task(
                task_factory(
                    wp.wp_id,
                    feature,
                    workspace_path,
                    prompt_path,
                    impl_agent_id,
                    host,
                    run_state,
                    agent_cfg,
                    cfg,
                    concurrency,
                )
            )
            active_tasks.add(task)
            task.add_done_callback(active_tasks.discard)

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


async def _run_review_task(
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
    """Wrapper that resumes review for an in-flight WP and releases the slot."""
    try:
        await _run_review_phase(
            wp_id=wp_id,
            feature=feature,
            workspace_path=workspace_path,
            prompt_path=prompt_path,
            impl_agent_id=impl_agent_id,
            host=host,
            run_state=run_state,
            agent_cfg=agent_cfg,
            cfg=cfg,
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
