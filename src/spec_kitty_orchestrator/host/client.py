"""HostClient: the only gateway between the provider and spec-kitty workflow state.

Host commands are executed via ``spec-kitty orchestrator-api``. Most host
commands accept ``--json`` explicitly, but some contract-compatible builds
emit the canonical JSON envelope by default and reject the flag. The client
prefers ``--json`` and transparently retries without it when needed.

The JSON response is parsed against the canonical envelope and validated.
Errors are mapped to typed HostError subclasses.

This module has no dependencies on the host's internal packages.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .models import (
    AcceptFeatureData,
    AppendHistoryData,
    ContractVersionData,
    FeatureStateData,
    HostResponse,
    ListReadyData,
    MergeData,
    StartImplData,
    StartReviewData,
    TransitionData,
)

# The minimum contract version this provider supports
_MIN_CONTRACT_VERSION = "1.0.0"
_SPEC_KITTY_BIN = "spec-kitty"
_UNSUPPORTED_JSON_FLAG = "No such option: --json"


class HostError(Exception):
    """Base class for all host API errors."""

    def __init__(self, error_code: str, message: str, data: dict[str, Any] | None = None):
        super().__init__(f"[{error_code}] {message}")
        self.error_code = error_code
        self.raw_data = data or {}


class ContractMismatchError(HostError):
    """Raised when the host contract version is incompatible."""


class FeatureNotFoundError(HostError):
    """Raised when the requested feature slug does not exist."""


class WPNotFoundError(HostError):
    """Raised when the requested WP does not exist."""


class TransitionRejectedError(HostError):
    """Raised when a lane transition is rejected by the state machine."""


class WPAlreadyClaimedError(HostError):
    """Raised when a WP is claimed by a different actor."""


class PolicyValidationError(HostError):
    """Raised when policy JSON is invalid or contains secrets."""


class FeatureNotReadyError(HostError):
    """Raised when accept-feature is called before all WPs are done."""


class PreflightFailedError(HostError):
    """Raised when merge-feature preflight checks fail."""


class TaskWorkflowError(HostError):
    """Raised when a `spec-kitty agent tasks` command fails."""


_ERROR_CODE_MAP: dict[str, type[HostError]] = {
    "CONTRACT_VERSION_MISMATCH": ContractMismatchError,
    "FEATURE_NOT_FOUND": FeatureNotFoundError,
    "WP_NOT_FOUND": WPNotFoundError,
    "TRANSITION_REJECTED": TransitionRejectedError,
    "WP_ALREADY_CLAIMED": WPAlreadyClaimedError,
    "POLICY_METADATA_REQUIRED": PolicyValidationError,
    "POLICY_VALIDATION_FAILED": PolicyValidationError,
    "FEATURE_NOT_READY": FeatureNotReadyError,
    "PREFLIGHT_FAILED": PreflightFailedError,
    "TASK_WORKFLOW_ERROR": TaskWorkflowError,
}


class HostClient:
    """Subprocess client for spec-kitty orchestrator-api.

    All host state mutations flow through this class. Instantiated once per
    orchestration run with a fixed actor identity and policy.

    Args:
        repo_root: Absolute path to the spec-kitty project root.
        actor: Actor identity string (e.g. "spec-kitty-orchestrator:claude-code").
        policy_json: Pre-serialized policy JSON string for mutation calls.
        bin_path: Override the spec-kitty binary path (for testing).
    """

    def __init__(
        self,
        repo_root: Path,
        actor: str,
        policy_json: str | None = None,
        bin_path: str = _SPEC_KITTY_BIN,
    ) -> None:
        self.repo_root = repo_root
        self.actor = actor
        self.policy_json = policy_json
        self._bin = bin_path

    def _call(self, args: list[str]) -> HostResponse:
        """Invoke spec-kitty orchestrator-api with the given args.

        Runs: spec-kitty orchestrator-api <args> [--json]
        Parses the canonical JSON envelope.
        Raises HostError (or subclass) on success=false.

        Args:
            args: Subcommand and its arguments (without the binary or group prefix).

        Returns:
            Validated HostResponse.

        Raises:
            ContractMismatchError: If the host reports CONTRACT_VERSION_MISMATCH.
            HostError: For any other error_code.
            RuntimeError: If subprocess fails entirely or output is not JSON.
        """
        def _run(include_json: bool) -> subprocess.CompletedProcess[str]:
            cmd = [self._bin, "orchestrator-api"] + args
            if include_json:
                cmd.append("--json")
            try:
                return subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    cwd=self.repo_root,
                )
            except FileNotFoundError as exc:
                raise RuntimeError(
                    f"spec-kitty binary not found at '{self._bin}'. "
                    "Is spec-kitty installed and on PATH?"
                ) from exc

        try:
            result = _run(include_json=True)
            if (
                result.returncode != 0
                and not result.stdout.strip()
                and _UNSUPPORTED_JSON_FLAG in result.stderr
            ):
                result = _run(include_json=False)
        except RuntimeError:
            raise

        raw_output = result.stdout.strip()
        if not raw_output:
            raise RuntimeError(
                f"spec-kitty orchestrator-api returned no output.\n"
                f"Exit code: {result.returncode}\nstderr: {result.stderr[:500]}"
            )

        try:
            envelope = json.loads(raw_output)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"spec-kitty orchestrator-api returned non-JSON output:\n{raw_output[:500]}"
            ) from exc

        response = HostResponse(**envelope)

        if not response.success:
            error_code = response.error_code or "UNKNOWN_ERROR"
            message = response.data.get("message", str(response.data))
            exc_class = _ERROR_CODE_MAP.get(error_code, HostError)
            raise exc_class(error_code, message, response.data)

        return response

    def _call_agent_tasks_json(self, args: list[str]) -> dict[str, Any]:
        """Invoke `spec-kitty agent tasks ... --json` and parse its JSON output."""
        cmd = [self._bin, "agent", "tasks"] + args + ["--json"]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                cwd=self.repo_root,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"spec-kitty binary not found at '{self._bin}'. "
                "Is spec-kitty installed and on PATH?"
            ) from exc

        raw_output = result.stdout.strip()
        if not raw_output:
            raise RuntimeError(
                f"spec-kitty agent tasks returned no output.\n"
                f"Exit code: {result.returncode}\nstderr: {result.stderr[:500]}"
            )

        payload: dict[str, Any] | None = None
        parsed_objects: list[dict[str, Any]] = []
        json_error: Exception | None = None

        for line in [ln.strip() for ln in raw_output.splitlines() if ln.strip()]:
            try:
                candidate = json.loads(line)
            except json.JSONDecodeError as exc:
                json_error = exc
                continue
            if isinstance(candidate, dict):
                parsed_objects.append(candidate)

        if parsed_objects:
            if result.returncode != 0:
                for candidate in parsed_objects:
                    error_value = candidate.get("error")
                    if isinstance(error_value, str) and error_value.strip() and error_value.strip() not in {"0", "1"}:
                        payload = candidate
                        break
            if payload is None:
                payload = parsed_objects[-1]

        if payload is None:
            raise RuntimeError(
                f"spec-kitty agent tasks returned non-JSON output:\n{raw_output[:500]}"
            ) from json_error

        if result.returncode != 0 or payload.get("error"):
            message = payload.get("error", "Task workflow command failed")
            error_data = payload if isinstance(payload, dict) else None
            if args and args[0] == "move-task":
                raise TransitionRejectedError(
                    "TRANSITION_REJECTED",
                    message,
                    error_data,
                )
            raise TaskWorkflowError(
                "TASK_WORKFLOW_ERROR",
                message,
                error_data,
            )

        if not isinstance(payload, dict):
            raise RuntimeError(
                f"spec-kitty agent tasks returned unexpected payload type: {type(payload)!r}"
            )

        return payload

    # ── Read commands ───────────────────────────────────────────────────────

    def contract_version(self) -> ContractVersionData:
        """Return the host API contract version info.

        Raises:
            ContractMismatchError: If the host contract version is older than
                the minimum version this provider requires.
        """
        resp = self._call(["contract-version"])
        data = ContractVersionData(**resp.data)

        host_ver = tuple(int(x) for x in data.api_version.split("."))
        min_ver = tuple(int(x) for x in _MIN_CONTRACT_VERSION.split("."))
        if host_ver < min_ver:
            raise ContractMismatchError(
                "CONTRACT_VERSION_MISMATCH",
                f"Host contract version {data.api_version!r} is below the minimum "
                f"required version {_MIN_CONTRACT_VERSION!r}. "
                "Upgrade spec-kitty on the host.",
            )

        return data

    def feature_state(self, feature: str) -> FeatureStateData:
        """Return full state of a feature (all WPs, lanes, deps).

        Args:
            feature: Feature slug (e.g. "034-my-feature").
        """
        resp = self._call(["feature-state", "--feature", feature])
        return FeatureStateData(**resp.data)

    def list_ready(self, feature: str) -> ListReadyData:
        """List WPs that are ready to start (planned + all deps done).

        Args:
            feature: Feature slug.
        """
        resp = self._call(["list-ready", "--feature", feature])
        return ListReadyData(**resp.data)

    # ── Mutation commands (require policy) ──────────────────────────────────

    def _require_policy(self) -> str:
        """Return policy JSON, raising if not configured."""
        if not self.policy_json:
            raise ValueError(
                "HostClient requires policy_json for mutation commands. "
                "Construct HostClient with policy_json= set."
            )
        return self.policy_json

    def start_implementation(self, feature: str, wp: str) -> StartImplData:
        """Composite transition planned→claimed→in_progress for a WP.

        Args:
            feature: Feature slug.
            wp: Work package ID (e.g. "WP01").
        """
        policy = self._require_policy()
        resp = self._call([
            "start-implementation",
            "--feature", feature,
            "--wp", wp,
            "--actor", self.actor,
            "--policy", policy,
        ])
        return StartImplData(**resp.data)

    def start_review(
        self, feature: str, wp: str, review_ref: str
    ) -> StartReviewData:
        """Transition a WP from for_review back to in_progress (review cycle).

        Args:
            feature: Feature slug.
            wp: Work package ID.
            review_ref: Opaque reference identifying the review feedback.
        """
        policy = self._require_policy()
        resp = self._call([
            "start-review",
            "--feature", feature,
            "--wp", wp,
            "--actor", self.actor,
            "--policy", policy,
            "--review-ref", review_ref,
        ])
        return StartReviewData(**resp.data)

    def transition(
        self,
        feature: str,
        wp: str,
        to: str,
        note: str | None = None,
        review_ref: str | None = None,
    ) -> TransitionData:
        """Emit a single lane transition for a WP.

        Policy is attached automatically when transitioning to run-affecting lanes.

        Args:
            feature: Feature slug.
            wp: Work package ID.
            to: Target lane name.
            note: Optional reason/note.
            review_ref: Optional review reference (for for_review→done).
        """
        args = [
            "transition",
            "--feature", feature,
            "--wp", wp,
            "--to", to,
            "--actor", self.actor,
        ]
        if note:
            args += ["--note", note]
        if self.policy_json:
            args += ["--policy", self.policy_json]
        if review_ref:
            args += ["--review-ref", review_ref]
        resp = self._call(args)
        return TransitionData(**resp.data)

    def move_task_for_review(
        self,
        feature: str,
        wp: str,
        agent: str,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Promote a WP to `for_review` via the task workflow CLI."""
        args = [
            "move-task",
            wp,
            "--feature", feature,
            "--to", "for_review",
            "--agent", agent,
        ]
        if note:
            args += ["--note", note]
        return self._call_agent_tasks_json(args)

    def mark_task_status(
        self,
        feature: str,
        task_ids: list[str],
        status: str,
    ) -> dict[str, Any]:
        """Update one or more subtask checkbox states in tasks.md."""
        if not task_ids:
            return {"result": "noop", "updated": []}
        args = [
            "mark-status",
            *task_ids,
            "--status",
            status,
            "--feature",
            feature,
        ]
        return self._call_agent_tasks_json(args)

    def append_history(
        self, feature: str, wp: str, note: str
    ) -> AppendHistoryData:
        """Append a history entry to a WP prompt file.

        Args:
            feature: Feature slug.
            wp: Work package ID.
            note: Text of the history entry.
        """
        resp = self._call([
            "append-history",
            "--feature", feature,
            "--wp", wp,
            "--actor", self.actor,
            "--note", note,
        ])
        return AppendHistoryData(**resp.data)

    def accept_feature(self, feature: str) -> AcceptFeatureData:
        """Accept a feature after all WPs are done.

        Args:
            feature: Feature slug.
        """
        resp = self._call([
            "accept-feature",
            "--feature", feature,
            "--actor", self.actor,
        ])
        return AcceptFeatureData(**resp.data)

    def merge_feature(
        self,
        feature: str,
        target: str = "main",
        strategy: str = "merge",
        push: bool = False,
    ) -> MergeData:
        """Run preflight checks then merge WP branches into target.

        Args:
            feature: Feature slug.
            target: Target branch (default: "main").
            strategy: Merge strategy: merge | squash | rebase.
            push: Whether to push target branch after merge.
        """
        args = [
            "merge-feature",
            "--feature", feature,
            "--target", target,
            "--strategy", strategy,
        ]
        if push:
            args.append("--push")
        resp = self._call(args)
        return MergeData(**resp.data)


__all__ = [
    "HostClient",
    "HostError",
    "ContractMismatchError",
    "FeatureNotFoundError",
    "WPNotFoundError",
    "TransitionRejectedError",
    "WPAlreadyClaimedError",
    "PolicyValidationError",
    "FeatureNotReadyError",
    "PreflightFailedError",
]
