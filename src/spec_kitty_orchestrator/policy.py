"""PolicyMetadata: orchestrator identity and capability declaration.

Built at orchestrator startup from config + CLI flags.
Passed to every HostClient call that mutates workflow state.
The host validates the policy and records it alongside every WP event.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass

_SECRET_PATTERN = re.compile(
    r"(token|secret|key|password|credential)", re.IGNORECASE
)

_VALID_APPROVAL_MODES = frozenset(["full_auto", "interactive", "supervised"])
_VALID_SANDBOX_MODES = frozenset(["workspace_write", "read_only", "none"])
_VALID_NETWORK_MODES = frozenset(["allowlist", "none", "open"])


@dataclass(frozen=True)
class PolicyMetadata:
    """Immutable orchestrator identity and capability declaration.

    Attributes:
        orchestrator_id: Unique orchestrator identifier (e.g. "spec-kitty-orchestrator").
        orchestrator_version: Package version string (e.g. "0.1.0").
        agent_family: Primary agent family used (e.g. "claude", "gemini").
        approval_mode: Human approval policy: full_auto | interactive | supervised.
        sandbox_mode: Filesystem access scope: workspace_write | read_only | none.
        network_mode: Network access policy: allowlist | none | open.
        dangerous_flags: List of dangerous flags explicitly allowed (must be empty in production).
        tool_restrictions: Optional comma-separated list of allowed tools.
    """

    orchestrator_id: str
    orchestrator_version: str
    agent_family: str
    approval_mode: str
    sandbox_mode: str
    network_mode: str
    dangerous_flags: list[str]
    tool_restrictions: str | None = None

    def validate(self) -> None:
        """Validate policy fields; raise ValueError if invalid.

        Rejects token-like values, validates enum fields.
        """
        if self.approval_mode not in _VALID_APPROVAL_MODES:
            raise ValueError(
                f"approval_mode must be one of {sorted(_VALID_APPROVAL_MODES)}, "
                f"got {self.approval_mode!r}"
            )
        if self.sandbox_mode not in _VALID_SANDBOX_MODES:
            raise ValueError(
                f"sandbox_mode must be one of {sorted(_VALID_SANDBOX_MODES)}, "
                f"got {self.sandbox_mode!r}"
            )
        if self.network_mode not in _VALID_NETWORK_MODES:
            raise ValueError(
                f"network_mode must be one of {sorted(_VALID_NETWORK_MODES)}, "
                f"got {self.network_mode!r}"
            )

        # Reject any field containing secret-like strings
        for field_name, value in asdict(self).items():
            if isinstance(value, str) and _SECRET_PATTERN.search(value):
                raise ValueError(
                    f"Policy field '{field_name}' appears to contain a secret. "
                    "Do not embed secrets in policy metadata."
                )

    def to_json(self) -> str:
        """Serialize to JSON string suitable for --policy CLI option."""
        return json.dumps(asdict(self))

    @classmethod
    def from_dict(cls, data: dict) -> "PolicyMetadata":
        """Construct from a plain dict (e.g. loaded from YAML config)."""
        return cls(
            orchestrator_id=str(data["orchestrator_id"]),
            orchestrator_version=str(data["orchestrator_version"]),
            agent_family=str(data["agent_family"]),
            approval_mode=str(data["approval_mode"]),
            sandbox_mode=str(data["sandbox_mode"]),
            network_mode=str(data["network_mode"]),
            dangerous_flags=list(data.get("dangerous_flags", [])),
            tool_restrictions=data.get("tool_restrictions"),
        )


__all__ = ["PolicyMetadata"]
