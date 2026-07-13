"""Main async orchestration loop.

Polls host for ready WPs, assigns agents, executes impl -> review -> done cycles.
All host state transitions go through HostClient. Provider-local state is
persisted via save_state after each significant event.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
import uuid
from pathlib import Path
from typing import Any

from .agents import get_invoker
from .config import AgentSelectionConfig, OrchestratorConfig
from .executor import CommitError, commit_lane_work, current_lane_head, execute_agent, get_log_path
from .host.client import HostClient, TransitionRejectedError, WPAlreadyClaimedError
from .monitor import (
    classify_failure,
    extract_review_feedback,
    is_success,
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


def _commit_implementation(
    workspace_path: Path,
    wp_id: str,
    agent_id: str,
    lane_branch: str | None,
    output_base: str | None,
) -> tuple[bool, str | None]:
    """Commit the implementer's lane edits after a successful run.

    ``output_base`` is the lane HEAD captured *before* this WP ran, so output is
    measured as commits this WP actually added — dependency-lane merges applied
    at allocation are not mistaken for WP output.

    Returns ``(ok, error)``:
    - ``(True, None)``  real output was committed, OR this is a legacy/non-lane
      WP (``lane_branch`` is None) for which the orchestrator does not commit.
    - ``(False, msg)``  the WP produced no committable output, or committing
      failed (e.g. the worktree is not on the lane branch). The caller must NOT
      advance the WP — empty WPs reaching ``done`` is the bug this guards.
    """
    if not lane_branch:
        return True, None
    try:
        has_output = commit_lane_work(
            workspace_path,
            f"feat({wp_id}): implementation by {agent_id}",
            lane_branch=lane_branch,
            output_base=output_base,
        )
    except CommitError as exc:
        return False, str(exc)
    if not has_output:
        return False, "implementation produced no committable changes"
    return True, None


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
    *,
    lane_branch: str | None = None,
    current_lane: str | None = None,
) -> None:
    """Execute one WP through the full impl -> (review ->)* done lifecycle.

    Handles retries and fallback for both implementation and review phases.
    Releases the concurrency slot when done (success or exhausted).

    Args:
        wp_id: Work package ID.
        mission: Mission slug.
        workspace_path: Lane worktree path returned by host.start_implementation.
        prompt_path: WP markdown prompt file path.
        impl_agent_id: Selected implementation agent ID.
        host: HostClient for all state mutations.
        run_state: Provider-local run state (mutated in-place).
        agent_cfg: Agent selection config.
        cfg: Full orchestrator config.
        concurrency: Concurrency manager (already acquired before this call).
        lane_branch: Lane branch the worktree is on (contract >= 1.1.0). When set,
            the orchestrator commits the implementer's edits onto it after each
            successful run. None for legacy/non-lane WPs (no commit performed).
    """
    wp_exec = run_state.get_or_create_wp(wp_id)
    wp_exec.implementation_agent = impl_agent_id
    save_state(run_state, cfg.state_file)

    # Lane tip before any of this WP's work runs. Output is measured against this
    # (NOT the mission base) so dependency-lane merges already applied when the
    # worktree was allocated are not miscounted as this WP's implementation.
    output_base = current_lane_head(workspace_path) if lane_branch else None

    try:
        prompt_text = prompt_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.error("Cannot read prompt %s: %s", prompt_path, exc)
        _mark_failed(wp_exec, str(exc))
        save_state(run_state, cfg.state_file)
        return

    subtask_ids = _extract_subtask_ids(prompt_text)
    if subtask_ids:
        prompt_text = _inject_subtask_tracking(prompt_text, subtask_ids, mission)

    # -- Implementation phase ----------------------------------------------
    # Defect 3: a WP resumed straight from for_review (by an interrupted run) is
    # already implemented — skip the implementation loop AND the for_review
    # transition below, and go directly to the review phase. Seeding impl_success
    # True makes the while-loop a no-op for that case.
    resuming_from_review = current_lane == "for_review"

    impl_success = resuming_from_review
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
            committed, commit_err = _commit_implementation(
                workspace_path, wp_id, impl_agent_id, lane_branch, output_base
            )
            if not committed:
                # Agent exited 0 but produced no committable output (or the
                # commit was unsafe). Do NOT advance — surface it instead.
                logger.warning("WP %s: not advancing to review — %s", wp_id, commit_err)
                host.append_history(mission, wp_id, f"FAILED: {commit_err}")
                _mark_failed(wp_exec, commit_err or "commit failed")
                save_state(run_state, cfg.state_file)
                return
            impl_success = True
            host.append_history(
                mission, wp_id,
                f"Implementation committed and completed by '{impl_agent_id}'"
            )
            output_base = current_lane_head(workspace_path) if lane_branch else output_base
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

    # Transition to for_review (skipped when resuming — the WP is already there).
    if not resuming_from_review:
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
    # Contract >= 1.2.0: claim the review lane before executing the reviewer so
    # the host records review ownership (for_review -> in_review).

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

        review_ref = f"review-{wp_id}-cycle{review_cycle}-{uuid.uuid4().hex[:8]}"
        try:
            start_review = host.start_review(mission, wp_id, review_ref=review_ref)
            if start_review.to_lane != "in_review":
                raise TransitionRejectedError(
                    "TRANSITION_REJECTED",
                    f"start-review returned unsupported lane '{start_review.to_lane}'",
                )
        except (TransitionRejectedError, WPAlreadyClaimedError) as exc:
            logger.error("WP %s: start-review failed: %s", wp_id, exc)
            _mark_failed(wp_exec, str(exc))
            save_state(run_state, cfg.state_file)
            return

        # Run review while the WP is owned by the review lane.
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
                _mark_failed(wp_exec, str(exc))
            break

        # Rejected -- extract feedback, enforce retry limit
        feedback = extract_review_feedback(review_result)
        wp_exec.review_feedback = feedback
        wp_exec.review_retries += 1
        save_state(run_state, cfg.state_file)
        feedback_ref = f"feedback-{wp_id}-cycle{review_cycle}-{uuid.uuid4().hex[:8]}"

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
                    review_result_json=_review_result_json(
                        review_agent_id,
                        "changes_requested",
                        feedback_ref,
                    ),
                )
            except TransitionRejectedError as exc:
                logger.error("WP %s: blocked transition rejected: %s", wp_id, exc)
                _mark_failed(wp_exec, str(exc))
            break

        host.append_history(
            mission, wp_id,
            f"Review cycle {review_cycle} rejected. Feedback: {(feedback or 'none')[:200]}"
        )

        try:
            _transition_review_rejected(
                host,
                mission,
                wp_id,
                review_agent_id,
                feedback_ref,
            )
        except TransitionRejectedError as exc:
            logger.error("WP %s: review rejection transition rejected: %s", wp_id, exc)
            _mark_failed(wp_exec, str(exc))
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
                )
            except TransitionRejectedError as exc:
                logger.error("WP %s: blocked transition rejected: %s", wp_id, exc)
                _mark_failed(wp_exec, str(exc))
            break

        # Commit the rework output before re-review (same gate as first impl).
        committed, commit_err = _commit_implementation(
            workspace_path, wp_id, impl_agent_id, lane_branch, output_base
        )
        if not committed:
            host.append_history(mission, wp_id, f"Re-implementation not accepted: {commit_err}")
            try:
                _host_transition(
                    host, mission, wp_id, "blocked",
                    note=f"Re-implementation not accepted: {commit_err}",
                )
            except TransitionRejectedError as exc:
                logger.error("WP %s: blocked transition rejected: %s", wp_id, exc)
                _mark_failed(wp_exec, str(exc))
            break
        output_base = current_lane_head(workspace_path) if lane_branch else output_base

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
            _mark_failed(wp_exec, str(exc))
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
    evidence_json: str | None = None,
    review_result_json: str | None = None,
    subtasks_complete: bool | None = None,
    implementation_evidence_present: bool | None = None,
) -> None:
    """Call transition with newer guard flags without requiring host/client edits."""
    extra_kwargs: dict[str, Any] = {}
    if force:
        extra_kwargs["force"] = True
    if evidence_json is not None:
        extra_kwargs["evidence_json"] = evidence_json
    if review_result_json is not None:
        extra_kwargs["review_result_json"] = review_result_json
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
        evidence_json=evidence_json,
        review_result_json=review_result_json,
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
    evidence_json: str | None,
    review_result_json: str | None,
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
    if evidence_json is not None:
        args += ["--evidence-json", evidence_json]
    if review_result_json is not None:
        args += ["--review-result-json", review_result_json]
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
    """Ask the host to validate and perform in_progress -> for_review."""
    _host_transition(host, mission, wp_id, "for_review", note=note)


def _review_result_json(reviewer: str, verdict: str, reference: str) -> str:
    return json.dumps(
        {"reviewer": reviewer, "verdict": verdict, "reference": reference},
        separators=(",", ":"),
        sort_keys=True,
    )


def _transition_review_approved(
    host: HostClient,
    mission: str,
    wp_id: str,
    review_agent_id: str,
    review_cycle: int,
    review_ref: str,
) -> None:
    """Ask the host to mark a reviewed WP done without bypassing its gates."""
    note = f"Review approved by '{review_agent_id}'"
    review_result_json = _review_result_json(
        review_agent_id,
        "approved",
        review_ref,
    )
    _host_transition(
        host,
        mission,
        wp_id,
        "done",
        note=note,
        review_ref=review_ref,
        review_result_json=review_result_json,
        evidence_json=json.dumps(
            {
                "review": {
                    "reviewer": review_agent_id,
                    "verdict": "approved",
                    "reference": review_ref,
                }
            },
            separators=(",", ":"),
            sort_keys=True,
        ),
    )


def _transition_review_rejected(
    host: HostClient,
    mission: str,
    wp_id: str,
    review_agent_id: str,
    feedback_ref: str,
) -> None:
    """Move a rejected WP from in_review back to in_progress for rework."""
    _host_transition(
        host,
        mission,
        wp_id,
        "in_progress",
        note="Review rejected; rework required",
        review_ref=feedback_ref,
        review_result_json=_review_result_json(
            review_agent_id,
            "changes_requested",
            feedback_ref,
        ),
    )


_FRONTMATTER_RE = re.compile(
    r"\A---[ \t]*\r?\n(?P<body>.*?)\r?\n---[ \t]*(?:\r?\n|\Z)",
    re.DOTALL,
)
_SUBTASK_KEY_RE = re.compile(r"^subtasks:\s*(?P<inline>\[[^\n]*\])?\s*$")
_SUBTASK_ITEM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")


def _normalize_subtask_id(value: str) -> str | None:
    """Strip YAML scalar quotes and reject values unsafe for CLI guidance."""
    candidate = value.strip()
    if len(candidate) >= 2 and candidate[0] == candidate[-1] and candidate[0] in {'"', "'"}:
        candidate = candidate[1:-1]
    return candidate if _SUBTASK_ITEM_RE.fullmatch(candidate) else None


def _extract_subtask_ids(prompt_text: str) -> list[str]:
    """Return normalized IDs from canonical block or inline YAML frontmatter."""
    frontmatter_match = _FRONTMATTER_RE.match(prompt_text)
    if not frontmatter_match:
        return []

    lines = frontmatter_match.group("body").splitlines()
    for index, line in enumerate(lines):
        key_match = _SUBTASK_KEY_RE.fullmatch(line.strip())
        if not key_match:
            continue

        inline = key_match.group("inline")
        raw_values: list[str]
        if inline is not None:
            raw_values = inline[1:-1].split(",") if inline[1:-1].strip() else []
        else:
            raw_values = []
            for item_line in lines[index + 1 :]:
                item_match = re.fullmatch(r"[ \t]*-[ \t]+(.+?)[ \t]*", item_line)
                if not item_match:
                    break
                raw_values.append(item_match.group(1))

        normalized = [_normalize_subtask_id(value) for value in raw_values]
        return [value for value in normalized if value is not None]
    return []


def _inject_subtask_tracking(prompt_text: str, subtask_ids: list[str], mission: str) -> str:
    """Prepend best-effort per-subtask mark-status guidance to the prompt.

    A cooperative agent calls ``mark-status`` after each completed subtask so
    dashboard progress updates during its WP-level run. The orchestrator does
    not mark anything itself: unchecked tasks remain genuine host-gate evidence.
    """
    if not subtask_ids:
        return prompt_text
    lines = [
        "## Agent Subtask Completion Protocol",
        "",
        "After completing **each** subtask, run this command immediately—do **not**",
        "wait until all subtasks are done:",
        "",
    ]
    for t_id in subtask_ids:
        lines.append(
            f"    spec-kitty agent tasks mark-status {shlex.quote(t_id)}"
            f" --status done --mission {shlex.quote(mission)}"
        )
    lines += [
        "",
        "Mark incrementally as you go so that dashboard progress is visible in real time.",
        "",
        "---",
        "",
    ]
    return "\n".join(lines) + prompt_text


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
        state_lanes = {wp.wp_id: wp.lane for wp in state_data.work_packages}

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

            current_lane = state_lanes.get(wp_id)

            if current_lane == "for_review":
                # Defect 3: resume a for_review WP straight into REVIEW. Resolve its
                # existing lane workspace READ-ONLY (start-implementation would
                # wrongly re-transition a for_review WP); execute_and_advance skips
                # the impl phase when current_lane == "for_review".
                try:
                    ws = host.resolve_workspace(mission, wp_id)
                except Exception as exc:
                    logger.error("WP %s: resolve-workspace failed: %s", wp_id, exc)
                    wp_exec = run_state.get_or_create_wp(wp_id)
                    wp_exec.last_error = truncate_error(str(exc))
                    save_state(run_state, cfg.state_file)
                    driven_ids.add(wp_id)  # quarantine — do not re-adopt-loop
                    continue
                workspace_path = Path(ws.workspace_path)
                prompt_path = Path(ws.prompt_path)
                lane_branch = ws.lane_branch
            else:
                # Claim (or resume, idempotently) the WP via host
                try:
                    impl_resp = host.start_implementation(mission, wp_id)
                except WPAlreadyClaimedError as exc:
                    logger.debug("WP %s already claimed by another actor, skipping", wp_id)
                    wp_exec = run_state.get_or_create_wp(wp_id)
                    wp_exec.last_error = truncate_error(str(exc))
                    save_state(run_state, cfg.state_file)
                    driven_ids.add(wp_id)
                    continue
                except Exception as exc:
                    # Surface the real error instead of silently collapsing to a
                    # downstream "deadlock": record it so it appears in the stall
                    # report if nothing else progresses.
                    logger.error("WP %s: start-implementation failed: %s", wp_id, exc)
                    wp_exec = run_state.get_or_create_wp(wp_id)
                    wp_exec.last_error = truncate_error(str(exc))
                    save_state(run_state, cfg.state_file)
                    # Quarantine so the WP is NOT re-adopted on the next poll. An
                    # un-allocatable lane (e.g. LANE_ALLOCATION_FAILED from a stale
                    # dirty worktree) would otherwise tight-loop every poll —
                    # spawning zero agents and starving other schedulable WPs.
                    # Marking it driven surfaces it in the stall report (with
                    # last_error) instead of spinning, while sibling WPs keep
                    # getting scheduled this iteration.
                    driven_ids.add(wp_id)
                    continue
                workspace_path = Path(impl_resp.workspace_path)
                prompt_path = Path(impl_resp.prompt_path)
                lane_branch = impl_resp.lane_branch

            driven_ids.add(wp_id)
            concurrency.mark_active(wp_id)
            await concurrency.acquire()

            task = asyncio.create_task(
                _run_wp_task(
                    wp_id, mission, workspace_path, prompt_path,
                    impl_agent_id, host, run_state, agent_cfg, cfg, concurrency,
                    lane_branch=lane_branch,
                    current_lane=current_lane,
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
    *,
    lane_branch: str | None = None,
    current_lane: str | None = None,
) -> None:
    """Wrapper that releases concurrency slot after execute_and_advance."""
    try:
        await execute_and_advance(
            wp_id, mission, workspace_path, prompt_path,
            impl_agent_id, host, run_state, agent_cfg, cfg, concurrency,
            lane_branch=lane_branch,
            current_lane=current_lane,
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
