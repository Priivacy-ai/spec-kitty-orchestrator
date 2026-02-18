"""Provider-local run state: what the host doesn't own.

Tracks retry counts, fallback agents tried, log paths, and timing.
Serialized to `.kittify/orchestrator-run-state.json`.

Lane/status fields are NEVER stored here — those are owned by the host.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .policy import PolicyMetadata

_STATE_FILENAME = "orchestrator-run-state.json"


@dataclass
class WPExecution:
    """Provider-local tracking for a single work package execution.

    Captures only what the host doesn't own: retry counts, agent choices,
    fallback history, log file paths, and timing.
    """

    wp_id: str
    implementation_agent: str | None = None
    implementation_retries: int = 0
    implementation_started_at: str | None = None
    implementation_completed_at: str | None = None
    review_agent: str | None = None
    review_retries: int = 0
    review_started_at: str | None = None
    review_completed_at: str | None = None
    review_feedback: str | None = None
    log_file: str | None = None
    fallback_agents_tried: list[str] = field(default_factory=list)
    restart_count: int = 0
    last_error: str | None = None


@dataclass
class RunState:
    """Provider-local state for an entire orchestration run.

    Attributes:
        run_id: Unique identifier for this run (ULID or UUID hex).
        feature_slug: The feature being orchestrated.
        started_at: ISO-8601 UTC timestamp when the run started.
        policy: The PolicyMetadata used for this run.
        wp_executions: Per-WP provider state keyed by WP ID.
    """

    run_id: str
    feature_slug: str
    started_at: str
    policy: PolicyMetadata
    wp_executions: dict[str, WPExecution] = field(default_factory=dict)

    def get_or_create_wp(self, wp_id: str) -> WPExecution:
        """Return existing WPExecution or create a new one."""
        if wp_id not in self.wp_executions:
            self.wp_executions[wp_id] = WPExecution(wp_id=wp_id)
        return self.wp_executions[wp_id]


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_run_state(feature_slug: str, policy: PolicyMetadata) -> RunState:
    """Create a new RunState for a fresh orchestration run."""
    import uuid

    return RunState(
        run_id=uuid.uuid4().hex,
        feature_slug=feature_slug,
        started_at=_now_utc(),
        policy=policy,
    )


def save_state(state: RunState, state_file: Path) -> None:
    """Atomically serialize RunState to JSON.

    Uses write-to-temp + rename for crash safety.

    Args:
        state: The RunState to persist.
        state_file: Target file path.
    """
    state_file.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "run_id": state.run_id,
        "feature_slug": state.feature_slug,
        "started_at": state.started_at,
        "policy": asdict(state.policy),
        "wp_executions": {
            wp_id: asdict(wp) for wp_id, wp in state.wp_executions.items()
        },
    }
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=state_file.parent, prefix=".tmp-orchestrator-state-"
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp_path, state_file)
    except Exception:
        os.unlink(tmp_path)
        raise


def load_state(state_file: Path) -> RunState | None:
    """Load RunState from a JSON file.

    Args:
        state_file: Path to the state JSON file.

    Returns:
        RunState if file exists and is valid, None otherwise.
    """
    if not state_file.exists():
        return None

    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        policy = PolicyMetadata.from_dict(data["policy"])
        wp_executions = {
            wp_id: WPExecution(**wp_data)
            for wp_id, wp_data in data.get("wp_executions", {}).items()
        }
        return RunState(
            run_id=data["run_id"],
            feature_slug=data["feature_slug"],
            started_at=data["started_at"],
            policy=policy,
            wp_executions=wp_executions,
        )
    except Exception:
        return None


__all__ = [
    "WPExecution",
    "RunState",
    "new_run_state",
    "save_state",
    "load_state",
]
