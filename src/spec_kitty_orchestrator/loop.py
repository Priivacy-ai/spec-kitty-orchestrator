"""Main async orchestration loop.

Polls host for ready WPs, assigns agents, executes impl → review → done cycles.
All host state transitions go through HostClient. Provider-local state is
persisted via save_state after each significant event.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import uuid
from datetime import datetime, timezone
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


def _now_utc() -> str:
    """Return an ISO-8601 UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


def _run_command(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run a subprocess command and capture text output."""
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=cwd,
    )


def _git_head(workspace_path: Path) -> str:
    """Return the current HEAD commit SHA for a worktree."""
    result = _run_command(["git", "rev-parse", "HEAD"], workspace_path)
    if result.returncode != 0:
        raise OrchestrationError(
            f"Failed to read HEAD for {workspace_path}: {result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout.strip()


def _git_status_lines(workspace_path: Path) -> list[str]:
    """Return porcelain status lines for a worktree."""
    result = _run_command(["git", "status", "--short"], workspace_path)
    if result.returncode != 0:
        raise OrchestrationError(
            f"Failed to inspect git status for {workspace_path}: {result.stderr.strip() or result.stdout.strip()}"
        )
    return [line for line in result.stdout.splitlines() if line.strip()]


def _commit_all_changes(workspace_path: Path, wp_id: str, context: str) -> str:
    """Stage and commit all worktree changes with required attribution."""
    add_result = _run_command(["git", "add", "-A"], workspace_path)
    if add_result.returncode != 0:
        raise OrchestrationError(
            f"Failed to stage changes for {wp_id}: {add_result.stderr.strip() or add_result.stdout.strip()}"
        )

    message = (
        f"feat({wp_id}): {context}\n\n"
        "Co-Authored-By: Codex GPT-5 <noreply@openai.com>\n"
    )
    commit_result = _run_command(["git", "commit", "-m", message], workspace_path)
    if commit_result.returncode != 0:
        raise OrchestrationError(
            f"Failed to commit changes for {wp_id}: {commit_result.stderr.strip() or commit_result.stdout.strip()}"
        )
    return _git_head(workspace_path)


def _extract_subtasks(prompt_text: str) -> list[str]:
    """Extract frontmatter subtask IDs from a WP prompt."""
    lines = prompt_text.splitlines()
    if not lines or lines[0].strip() != "---":
        return []

    try:
        end_idx = lines[1:].index("---") + 1
    except ValueError:
        return []

    subtasks: list[str] = []
    in_subtasks = False
    for line in lines[1:end_idx]:
        if line.startswith("subtasks:"):
            in_subtasks = True
            continue
        if not in_subtasks:
            continue
        if line.startswith("- "):
            subtasks.append(line[2:].strip())
            continue
        if line.strip():
            break
    return subtasks


def _sanitize_prompt_paths(prompt_text: str, repo_root: Path, workspace_path: Path) -> str:
    """Rewrite absolute repo-root references to the assigned worktree path."""
    repo_root_str = str(repo_root.resolve())
    workspace_root_str = str(workspace_path.resolve())
    rewritten = prompt_text.replace(repo_root_str, workspace_root_str)
    guardrail = (
        "\n\n## Orchestrator Guardrails\n\n"
        f"- You are running inside the assigned worktree: `{workspace_root_str}`.\n"
        "- Do not edit files outside the current worktree.\n"
        "- Complete implementation in the worktree, leave it commit-ready, and do not claim success without real file changes.\n"
    )
    return rewritten + guardrail


def _workspace_metadata_base_commit(repo_root: Path, feature: str, wp_id: str) -> str | None:
    """Return the recorded base commit for a host-managed workspace, if any."""
    metadata_path = repo_root / ".kittify" / "workspaces" / f"{feature}-{wp_id}.json"
    if not metadata_path.exists():
        return None
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    base_commit = payload.get("base_commit")
    return str(base_commit) if base_commit else None


def _workspace_requires_rebootstrap(repo_root: Path, feature: str, wp_id: str, workspace_path: Path) -> bool:
    """Return True when an existing workspace no longer matches its recorded base."""
    if not workspace_path.exists():
        return False

    try:
        status_lines = _git_status_lines(workspace_path)
    except OrchestrationError:
        return True

    if status_lines:
        return True

    recorded_base = _workspace_metadata_base_commit(repo_root, feature, wp_id)
    if not recorded_base:
        return False

    try:
        return _git_head(workspace_path) != recorded_base
    except OrchestrationError:
        return True


def _refresh_workspace(repo_root: Path, feature: str, wp_id: str, workspace_path: Path) -> Path:
    """Reset a provider-owned workspace back to its recorded base commit."""
    recorded_base = _workspace_metadata_base_commit(repo_root, feature, wp_id)
    reset_target = recorded_base or "HEAD"

    for cmd in (
        ["git", "reset", "--hard", reset_target],
        ["git", "clean", "-fd"],
    ):
        result = _run_command(cmd, workspace_path)
        if result.returncode != 0:
            raise OrchestrationError(
                f"Failed to refresh workspace for {wp_id}: {result.stderr.strip() or result.stdout.strip()}"
            )

    if _git_status_lines(workspace_path):
        raise OrchestrationError(f"Workspace for {wp_id} remained dirty after refresh")

    if recorded_base and _git_head(workspace_path) != recorded_base:
        raise OrchestrationError(
            f"Workspace for {wp_id} did not reset to recorded base {recorded_base}"
        )

    return workspace_path


def _finalize_successful_implementation(
    host: HostClient,
    workspace_path: Path,
    feature: str,
    wp_id: str,
    prompt_text: str,
    baseline_head: str,
    context: str,
) -> str:
    """Require real worktree evidence, then reconcile commit and task state."""
    dirty_before = _git_status_lines(workspace_path)
    head_before = _git_head(workspace_path)
    if not dirty_before and head_before == baseline_head:
        raise OrchestrationError(
            f"{wp_id} reported success without any worktree changes or commits"
        )

    if dirty_before:
        _commit_all_changes(workspace_path, wp_id, context)

    host.mark_subtasks_done(feature, _extract_subtasks(prompt_text))

    dirty_after = _git_status_lines(workspace_path)
    if dirty_after:
        _commit_all_changes(workspace_path, wp_id, f"{context} follow-up")
    return _git_head(workspace_path)


def _ensure_workspace_exists(repo_root: Path, feature: str, wp_id: str, workspace_path: Path) -> Path:
    """Materialize or refresh the host-managed workspace when required."""
    base_cmd = [
        "spec-kitty",
        "implement",
        wp_id,
        "--feature",
        feature,
        "--json",
    ]

    if workspace_path.exists():
        if not _workspace_requires_rebootstrap(repo_root, feature, wp_id, workspace_path):
            return workspace_path
        return _refresh_workspace(repo_root, feature, wp_id, workspace_path)

    attempts = [base_cmd[:-1] + ["--force", "--json"], base_cmd]
    last_error = "workspace bootstrap failed"

    for cmd in attempts:
        result = _run_command(cmd, repo_root)
        if result.returncode == 0:
            try:
                payload = json.loads(result.stdout.strip())
            except json.JSONDecodeError:
                payload = {}
            resolved = payload.get("workspace_path") or payload.get("workspace")
            if resolved:
                return (repo_root / resolved).resolve()
            if workspace_path.exists():
                return workspace_path
            last_error = "implement succeeded but workspace path was still missing"
            continue

        stderr = result.stderr.strip()
        stdout = result.stdout.strip()
        last_error = stderr or stdout or f"exit code {result.returncode}"

    raise OrchestrationError(
        f"Failed to create workspace for {wp_id} at {workspace_path}: {last_error}"
    )


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
    prompt_text = _sanitize_prompt_paths(prompt_text, cfg.repo_root, workspace_path)
    baseline_head = _git_head(workspace_path)

    # ── Implementation phase ──────────────────────────────────────────────

    impl_success = False
    while not impl_success:
        invoker = get_invoker(impl_agent_id)
        log_file = get_log_path(cfg.log_dir, feature, wp_id, "implementation")
        wp_exec.log_file = str(log_file)
        wp_exec.implementation_started_at = wp_exec.implementation_started_at or _now_utc()
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
            try:
                baseline_head = _finalize_successful_implementation(
                    host=host,
                    workspace_path=workspace_path,
                    feature=feature,
                    wp_id=wp_id,
                    prompt_text=prompt_text,
                    baseline_head=baseline_head,
                    context="implementation output",
                )
            except OrchestrationError as exc:
                logger.warning("WP %s: implementation finalization failed: %s", wp_id, exc)
                wp_exec.last_error = truncate_error(str(exc))
                host.append_history(feature, wp_id, f"FAILED: {exc}")
                try:
                    host.transition(feature, wp_id, "blocked", note=str(exc))
                except Exception:
                    pass
                save_state(run_state, cfg.state_file)
                return
            impl_success = True
            wp_exec.implementation_completed_at = _now_utc()
            wp_exec.last_error = None
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
        host.emit_status_transition(
            feature,
            wp_id,
            "for_review",
            subtasks_complete=True,
            implementation_evidence_present=True,
        )
        host.append_history(feature, wp_id, f"Implementation by '{impl_agent_id}' complete")
    except TransitionRejectedError as exc:
        logger.warning("WP %s: for_review transition rejected: %s", wp_id, exc)
        message = f"Review handoff rejected after successful implementation: {exc}"
        _mark_failed(wp_exec, message)
        host.append_history(feature, wp_id, f"FAILED: {message}")
        try:
            host.transition(feature, wp_id, "blocked", note=message)
        except Exception:
            pass
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
        wp_exec.log_file = str(get_log_path(cfg.log_dir, feature, wp_id, f"review-{review_cycle}"))
        review_log = get_log_path(cfg.log_dir, feature, wp_id, f"review-{review_cycle}")
        wp_exec.review_started_at = wp_exec.review_started_at or _now_utc()
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
                host.emit_status_transition(
                    feature,
                    wp_id,
                    "done",
                    review_ref=review_ref,
                    evidence={
                        "review": {
                            "reviewer": review_agent_id,
                            "verdict": "approved",
                            "reference": review_ref,
                        }
                    },
                )
                review_done = True
                wp_exec.review_completed_at = _now_utc()
                wp_exec.last_error = None
                save_state(run_state, cfg.state_file)
                host.append_history(feature, wp_id, f"Review approved in cycle {review_cycle}")
                logger.info("WP %s completed successfully", wp_id)
            except TransitionRejectedError as exc:
                message = f"Review approval could not be recorded: {exc}"
                wp_exec.last_error = truncate_error(message)
                save_state(run_state, cfg.state_file)
                host.append_history(feature, wp_id, f"FAILED: {message}")
                try:
                    host.transition(feature, wp_id, "blocked", note=message)
                except Exception:
                    pass
                logger.error("WP %s: done transition rejected: %s", wp_id, exc)
            break

        # Rejected — extract feedback, enforce retry limit
        feedback = extract_review_feedback(review_result)
        wp_exec.review_feedback = feedback
        wp_exec.review_retries += 1
        wp_exec.last_error = truncate_error(feedback or "review rejected")
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

        try:
            baseline_head = _finalize_successful_implementation(
                host=host,
                workspace_path=workspace_path,
                feature=feature,
                wp_id=wp_id,
                prompt_text=prompt_text,
                baseline_head=baseline_head,
                context=f"review cycle {review_cycle} reimplementation",
            )
        except OrchestrationError as exc:
            logger.error("WP %s: reimplementation finalization failed: %s", wp_id, exc)
            host.append_history(feature, wp_id, f"FAILED: {exc}")
            try:
                host.transition(feature, wp_id, "blocked", note=str(exc))
            except Exception:
                pass
            break

        # in_progress → for_review (back to review queue for next cycle)
        try:
            host.emit_status_transition(
                feature,
                wp_id,
                "for_review",
                subtasks_complete=True,
                implementation_evidence_present=True,
            )
            host.append_history(feature, wp_id, f"Re-implementation complete (cycle {review_cycle})")
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
            try:
                workspace_path = _ensure_workspace_exists(
                    cfg.repo_root, feature, wp.wp_id, workspace_path
                )
            except OrchestrationError as exc:
                logger.error("WP %s: workspace preparation failed: %s", wp.wp_id, exc)
                wp_exec = run_state.get_or_create_wp(wp.wp_id)
                wp_exec.last_error = truncate_error(str(exc))
                save_state(run_state, cfg.state_file)
                host.append_history(feature, wp.wp_id, f"FAILED: {exc}")
                try:
                    host.transition(feature, wp.wp_id, "blocked", note=str(exc))
                except Exception:
                    pass
                continue
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
