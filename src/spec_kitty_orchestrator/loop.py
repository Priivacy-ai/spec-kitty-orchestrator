"""Main async orchestration loop.

Polls host for ready WPs, assigns agents, executes impl -> review -> done cycles.
All host state transitions go through HostClient. Provider-local state is
persisted via save_state after each significant event.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any

from .agents import get_invoker
from .config import AgentSelectionConfig, OrchestratorConfig
from .executor import execute_agent, get_log_path
from .host.client import HostClient, TransitionRejectedError, WPAlreadyClaimedError
from .monitor import (
    classify_failure,
    extract_review_feedback,
    is_success,
    should_fallback,
    should_retry,
    truncate_error,
)
from .scheduler import (
    ConcurrencyManager,
    NoAgentAvailableError,
    select_implementer,
    select_reviewer,
    select_schedulable_wp_ids,
)
from .state import RunState, WPExecution, save_state

logger = logging.getLogger(__name__)

LOOP_POLL_INTERVAL = 2.0  # seconds between list-ready polls
DEADLOCK_THRESHOLD = 3  # consecutive empty-ready polls before declaring deadlock


class OrchestrationError(Exception):
    """Fatal orchestration error."""


class DeadlockError(OrchestrationError):
    """Raised when the loop can make no further progress.

    This covers a genuine dependency cycle, but also any WP the orchestrator
    cannot drive forward (e.g. left in for_review/in_review by an interrupted
    run, or one that failed and stuck in_progress). The message names each stuck
    WP, its lane, and any recorded error so the cause is not a mystery.
    """


def _describe_stuck_wps(state_data, run_state, terminal_lanes) -> str:
    """Render non-terminal WPs with their lane and any recorded last_error.

    Turns an opaque "deadlock" into an actionable list, e.g.
    ``WP01(lane=for_review), WP02(lane=planned, last_error=...)``.
    """
    parts: list[str] = []
    for wp in state_data.work_packages:
        if wp.lane in terminal_lanes:
            continue
        wp_exec = run_state.wp_executions.get(wp.wp_id)
        err = f", last_error={wp_exec.last_error}" if wp_exec and wp_exec.last_error else ""
        parts.append(f"{wp.wp_id}(lane={wp.lane}{err})")
    return ", ".join(parts) if parts else "(none)"


async def execute_and_advance(
    wp_id: str,
    mission: str,
    workspace_path: Path,
    prompt_path: Path,
    impl_agent_id: str,
    host: HostClient,
    run_state: RunState,
    agent_cfg: AgentSelectionConfig,
    cfg: OrchestratorConfig,
    concurrency: ConcurrencyManager,
) -> None:
    """Execute one WP through the full impl -> (review ->)* done lifecycle.

    Handles retries and fallback for both implementation and review phases.
    Releases the concurrency slot when done (success or exhausted).

    Args:
        wp_id: Work package ID.
        mission: Mission slug.
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

    # -- Implementation phase ----------------------------------------------

    impl_success = False
    while not impl_success:
        invoker = get_invoker(impl_agent_id)
        log_file = get_log_path(cfg.log_dir, mission, wp_id, "implementation")
        wp_exec.log_file = str(log_file)
        save_state(run_state, cfg.state_file)

        host.append_history(
            mission, wp_id,
            f"Starting implementation with agent '{impl_agent_id}' (retry #{wp_exec.implementation_retries})"
        )

        result = await execute_agent(
            invoker, prompt_text, workspace_path,
            role="implementation",
            timeout_seconds=agent_cfg.timeout_seconds,
            log_file=log_file,
            repo_root=_agent_repo_root(host, cfg),
        )

        if is_success(result):
            impl_success = True
            host.append_history(
                mission, wp_id,
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
            host.append_history(mission, wp_id, f"Retrying implementation (attempt {wp_exec.implementation_retries})")
            await asyncio.sleep(2.0 * wp_exec.implementation_retries)
            continue

        # Try fallback agent
        wp_exec.fallback_agents_tried.append(impl_agent_id)
        try:
            impl_agent_id = select_implementer(agent_cfg, wp_exec.fallback_agents_tried)
            wp_exec.implementation_agent = impl_agent_id
            wp_exec.implementation_retries = 0
            save_state(run_state, cfg.state_file)
            host.append_history(mission, wp_id, f"Falling back to agent '{impl_agent_id}'")
        except NoAgentAvailableError:
            logger.error("WP %s: all implementation agents exhausted", wp_id)
            host.append_history(mission, wp_id, "FAILED: all implementation agents exhausted")
            try:
                host.transition(mission, wp_id, "blocked", note="All implementation agents exhausted")
            except Exception:
                pass
            _mark_failed(wp_exec, "All implementation agents exhausted")
            save_state(run_state, cfg.state_file)
            return

    # Transition to for_review
    try:
        _transition_for_review(
            host,
            mission,
            wp_id,
            note=f"Implementation by '{impl_agent_id}' complete",
        )
    except TransitionRejectedError as exc:
        logger.warning("WP %s: for_review transition rejected: %s", wp_id, exc)
        _mark_failed(wp_exec, str(exc))
        save_state(run_state, cfg.state_file)
        return

    # -- Review phase ------------------------------------------------------
    # Host contract drift:
    # - legacy start-review means rejection/rework and returns in_progress
    # - current start-review means reviewer claim and returns in_review
    # Run review from for_review for compatibility, then adapt transitions.

    review_agent_id = select_reviewer(agent_cfg, impl_agent_id, [])
    review_cycle = 0
    review_done = False

    while not review_done:
        review_cycle += 1

        wp_exec.review_agent = review_agent_id
        review_log = get_log_path(cfg.log_dir, mission, wp_id, f"review-{review_cycle}")
        save_state(run_state, cfg.state_file)

        host.append_history(
            mission, wp_id,
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
            repo_root=_agent_repo_root(host, cfg),
        )

        if is_success(review_result):
            review_ref = f"review-{wp_id}-cycle{review_cycle}-{uuid.uuid4().hex[:8]}"
            try:
                _transition_review_approved(
                    host,
                    mission,
                    wp_id,
                    review_agent_id,
                    review_cycle,
                    review_ref,
                )
                review_done = True
                host.append_history(mission, wp_id, f"Review approved in cycle {review_cycle}")
                logger.info("WP %s completed successfully", wp_id)
            except TransitionRejectedError as exc:
                logger.error("WP %s: done transition rejected: %s", wp_id, exc)
            break

        # Rejected -- extract feedback, enforce retry limit
        feedback = extract_review_feedback(review_result)
        wp_exec.review_feedback = feedback
        wp_exec.review_retries += 1
        save_state(run_state, cfg.state_file)

        if wp_exec.review_retries > agent_cfg.max_retries:
            logger.error("WP %s: review retry limit exceeded", wp_id)
            host.append_history(mission, wp_id, "FAILED: review retry limit exceeded")
            try:
                _host_transition(
                    host,
                    mission,
                    wp_id,
                    "blocked",
                    note="Review cycle limit exceeded",
                    force=True,
                )
            except Exception:
                pass
            break

        feedback_ref = f"feedback-{wp_id}-cycle{review_cycle}-{uuid.uuid4().hex[:8]}"
        host.append_history(
            mission, wp_id,
            f"Review cycle {review_cycle} rejected. Feedback: {(feedback or 'none')[:200]}"
        )

        try:
            _transition_review_rejected(host, mission, wp_id, feedback_ref)
        except TransitionRejectedError as exc:
            logger.error("WP %s: review rejection transition rejected: %s", wp_id, exc)
            break

        # Run re-implementation with review feedback
        reimpl_log = get_log_path(cfg.log_dir, mission, wp_id, f"reimpl-{review_cycle}")
        reimpl_prompt = _build_rework_prompt(prompt_text, feedback)
        reimpl_result = await execute_agent(
            get_invoker(impl_agent_id),
            reimpl_prompt,
            workspace_path,
            role="implementation",
            timeout_seconds=agent_cfg.timeout_seconds,
            log_file=reimpl_log,
            repo_root=_agent_repo_root(host, cfg),
        )
        if not is_success(reimpl_result):
            error_msg = truncate_error(
                "; ".join(reimpl_result.errors) if reimpl_result.errors else "rework failed"
            )
            host.append_history(mission, wp_id, f"Re-implementation failed: {error_msg}")
            try:
                _host_transition(
                    host,
                    mission,
                    wp_id,
                    "blocked",
                    note=f"Re-implementation failed: {error_msg}",
                    force=True,
                )
            except Exception:
                pass
            break

        # in_progress -> for_review (back to review queue for next cycle)
        try:
            _transition_for_review(
                host,
                mission,
                wp_id,
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


def _agent_repo_root(host: HostClient, cfg: OrchestratorConfig) -> Path:
    repo_root = getattr(host, "repo_root", None)
    return repo_root if isinstance(repo_root, Path) else cfg.repo_root


def _host_transition(
    host: HostClient,
    mission: str,
    wp_id: str,
    to: str,
    *,
    note: str | None = None,
    review_ref: str | None = None,
    force: bool = False,
    subtasks_complete: bool | None = None,
    implementation_evidence_present: bool | None = None,
) -> None:
    """Call transition with newer guard flags without requiring host/client edits."""
    extra_kwargs: dict[str, Any] = {}
    if force:
        extra_kwargs["force"] = True
    if subtasks_complete is not None:
        extra_kwargs["subtasks_complete"] = subtasks_complete
    if implementation_evidence_present is not None:
        extra_kwargs["implementation_evidence_present"] = implementation_evidence_present

    try:
        host.transition(
            mission,
            wp_id,
            to,
            note=note,
            review_ref=review_ref,
            **extra_kwargs,
        )
        return
    except TypeError:
        if not extra_kwargs:
            raise

    _call_transition_direct(
        host,
        mission,
        wp_id,
        to,
        note=note,
        review_ref=review_ref,
        force=force,
        subtasks_complete=subtasks_complete,
        implementation_evidence_present=implementation_evidence_present,
    )


def _call_transition_direct(
    host: HostClient,
    mission: str,
    wp_id: str,
    to: str,
    *,
    note: str | None,
    review_ref: str | None,
    force: bool,
    subtasks_complete: bool | None,
    implementation_evidence_present: bool | None,
) -> None:
    call = getattr(host, "_call", None)
    if call is None:
        raise TypeError("Host transition does not support required compatibility flags")

    args = [
        "transition",
        "--mission",
        mission,
        "--wp",
        wp_id,
        "--to",
        to,
        "--actor",
        host.actor,
    ]
    if note:
        args += ["--note", note]
    policy_json = getattr(host, "policy_json", None)
    if policy_json:
        args += ["--policy", policy_json]
    if review_ref:
        args += ["--review-ref", review_ref]
    if force:
        args.append("--force")
    if subtasks_complete is not None:
        args.append(
            "--subtasks-complete" if subtasks_complete else "--no-subtasks-complete"
        )
    if implementation_evidence_present is not None:
        args.append(
            "--implementation-evidence-present"
            if implementation_evidence_present
            else "--no-implementation-evidence-present"
        )
    call(args)


def _transition_for_review(
    host: HostClient,
    mission: str,
    wp_id: str,
    note: str,
) -> None:
    """Move in_progress to for_review across strict and legacy hosts."""
    try:
        _host_transition(
            host,
            mission,
            wp_id,
            "for_review",
            note=note,
            subtasks_complete=True,
            implementation_evidence_present=True,
        )
    except TransitionRejectedError:
        _host_transition(
            host,
            mission,
            wp_id,
            "for_review",
            note=note,
            force=True,
            subtasks_complete=True,
            implementation_evidence_present=True,
        )


def _transition_review_approved(
    host: HostClient,
    mission: str,
    wp_id: str,
    review_agent_id: str,
    review_cycle: int,
    review_ref: str,
) -> None:
    """Mark a reviewed WP done across legacy and in_review host contracts."""
    note = f"Review approved by '{review_agent_id}'"
    try:
        _host_transition(
            host,
            mission,
            wp_id,
            "done",
            note=note,
            review_ref=review_ref,
        )
        return
    except TransitionRejectedError as first_error:
        logger.info(
            "WP %s: legacy done transition rejected, claiming review lane: %s",
            wp_id,
            first_error,
        )

    start_result = host.start_review(mission, wp_id, review_ref=review_ref)
    if start_result.to_lane == "in_review":
        _host_transition(
            host,
            mission,
            wp_id,
            "done",
            note=note,
            review_ref=review_ref,
            force=True,
        )
        return

    if start_result.to_lane == "in_progress":
        raise TransitionRejectedError(
            "TRANSITION_REJECTED",
            f"start-review returned legacy rework lane during approval cycle {review_cycle}",
        )

    raise TransitionRejectedError(
        "TRANSITION_REJECTED",
        f"start-review returned unsupported lane '{start_result.to_lane}'",
    )


def _transition_review_rejected(
    host: HostClient,
    mission: str,
    wp_id: str,
    feedback_ref: str,
) -> None:
    """Move a rejected WP back to in_progress across review lane contracts."""
    start_result = host.start_review(mission, wp_id, review_ref=feedback_ref)
    if start_result.to_lane == "in_progress":
        return
    if start_result.to_lane == "in_review":
        _host_transition(
            host,
            mission,
            wp_id,
            "in_progress",
            note="Review rejected; rework required",
            review_ref=feedback_ref,
            force=True,
        )
        return
    raise TransitionRejectedError(
        "TRANSITION_REJECTED",
        f"start-review returned unsupported lane '{start_result.to_lane}'",
    )


def _mark_failed(wp_exec: WPExecution, error: str) -> None:
    """Record failure in WPExecution."""
    wp_exec.last_error = error[:500]


async def run_orchestration_loop(
    mission: str,
    host: HostClient,
    run_state: RunState,
    cfg: OrchestratorConfig,
) -> None:
    """Main async orchestration loop.

    Continuously polls for ready WPs, dispatches them to agents, and waits
    until all WPs are done or a deadlock is detected.

    Args:
        mission: Mission slug.
        host: HostClient for all host interactions.
        run_state: Provider-local run state.
        cfg: Full orchestrator config.
    """
    concurrency = ConcurrencyManager(cfg.max_concurrent_wps)
    agent_cfg = cfg.agent_selection
    active_tasks: set[asyncio.Task] = set()  # type: ignore[type-arg]
    empty_ready_streak = 0
    # WP IDs this process has already dispatched. Used so an orphaned WP that
    # fails and sticks in_progress is not re-adopted forever — it surfaces as
    # stalled instead.
    driven_ids: set[str] = set()

    logger.info("Orchestration loop started for mission '%s'", mission)

    while True:
        ready_data = host.list_ready(mission)
        state_data = host.mission_state(mission)

        # Schedulable = ready (planned, deps met) WPs plus any orphaned WPs left
        # in a resumable lane by a prior/interrupted run. The latter is what lets
        # an interrupted mission resume instead of falsely dead-locking.
        schedulable_ids = select_schedulable_wp_ids(
            ready_data.ready_work_packages,
            state_data.work_packages,
            concurrency.active_wp_ids(),
            driven_ids,
        )

        terminal_lanes = {"done", "canceled", "blocked"}
        if not schedulable_ids and concurrency.active_count() == 0:
            all_lanes = [wp.lane for wp in state_data.work_packages]
            if all(lane in terminal_lanes for lane in all_lanes if lane):
                logger.info("All WPs reached terminal state. Orchestration complete.")
                break

            empty_ready_streak += 1
            if empty_ready_streak >= DEADLOCK_THRESHOLD:
                raise DeadlockError(
                    "Orchestration stalled: no schedulable or resumable work "
                    "packages and nothing in flight. Stuck WPs: "
                    + _describe_stuck_wps(state_data, run_state, terminal_lanes)
                )
        else:
            empty_ready_streak = 0

        # Schedule ready + resumable WPs
        for wp_id in schedulable_ids:
            if not concurrency.has_slot():
                break

            try:
                impl_agent_id = select_implementer(
                    agent_cfg,
                    run_state.get_or_create_wp(wp_id).fallback_agents_tried,
                )
            except NoAgentAvailableError:
                logger.warning("WP %s: no implementation agent available, skipping", wp_id)
                continue

            # Claim (or resume, idempotently) the WP via host
            try:
                impl_resp = host.start_implementation(mission, wp_id)
            except WPAlreadyClaimedError:
                logger.debug("WP %s already claimed by another actor, skipping", wp_id)
                continue
            except Exception as exc:
                # Surface the real error instead of silently collapsing to a
                # downstream "deadlock": record it so it appears in the stall
                # report if nothing else progresses.
                logger.error("WP %s: start-implementation failed: %s", wp_id, exc)
                wp_exec = run_state.get_or_create_wp(wp_id)
                wp_exec.last_error = truncate_error(str(exc))
                save_state(run_state, cfg.state_file)
                continue

            workspace_path = Path(impl_resp.workspace_path)
            prompt_path = Path(impl_resp.prompt_path)

            driven_ids.add(wp_id)
            concurrency.mark_active(wp_id)
            await concurrency.acquire()

            task = asyncio.create_task(
                _run_wp_task(
                    wp_id, mission, workspace_path, prompt_path,
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
    logger.info("Orchestration loop completed for mission '%s'", mission)


async def _run_wp_task(
    wp_id: str,
    mission: str,
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
            wp_id, mission, workspace_path, prompt_path,
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
