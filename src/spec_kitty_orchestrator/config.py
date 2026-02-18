"""Orchestrator configuration loaded from YAML or CLI flags.

Reads from `.kittify/orchestrator.yaml` (if present) and can be overridden
by CLI flags. Uses only stdlib + pydantic; no host-internal packages.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class FallbackStrategy(str, Enum):
    """Agent fallback strategy when the primary agent fails."""

    NEXT_AVAILABLE = "next_available"
    SAME_FAMILY = "same_family"
    NONE = "none"


@dataclass
class AgentSelectionConfig:
    """Configuration for agent assignment per role.

    Attributes:
        implementation_agents: Ordered list of agent IDs to try for implementation.
        review_agents: Ordered list of agent IDs to try for review.
        fallback_strategy: What to do when the primary agent fails.
        max_retries: Maximum retry attempts per WP per role.
        timeout_seconds: Maximum execution time per agent run.
        single_agent_mode: If True, use same agent for both impl and review.
    """

    implementation_agents: list[str] = field(default_factory=lambda: ["claude-code"])
    review_agents: list[str] = field(default_factory=lambda: ["claude-code"])
    fallback_strategy: FallbackStrategy = FallbackStrategy.NEXT_AVAILABLE
    max_retries: int = 2
    timeout_seconds: int = 3600  # 1 hour
    single_agent_mode: bool = False

    def select_implementer(
        self,
        tried: list[str] | None = None,
    ) -> str | None:
        """Select the next implementation agent, skipping already-tried ones.

        Args:
            tried: List of agent IDs already tried for this WP.

        Returns:
            Agent ID to use, or None if all exhausted.
        """
        tried_set = set(tried or [])
        for agent_id in self.implementation_agents:
            if agent_id not in tried_set:
                return agent_id
        return None

    def select_reviewer(
        self,
        impl_agent: str | None = None,
        tried: list[str] | None = None,
    ) -> str | None:
        """Select the next review agent.

        In single_agent_mode returns the same agent as implementation.

        Args:
            impl_agent: The implementation agent (used in single_agent_mode).
            tried: List of agent IDs already tried for review.

        Returns:
            Agent ID to use, or None if all exhausted.
        """
        if self.single_agent_mode and impl_agent:
            return impl_agent
        tried_set = set(tried or [])
        for agent_id in self.review_agents:
            if agent_id not in tried_set:
                return agent_id
        return None


@dataclass
class OrchestratorConfig:
    """Top-level orchestrator configuration.

    Attributes:
        repo_root: Absolute path to the spec-kitty project root.
        actor: Actor identity string passed to host API calls.
        max_concurrent_wps: Maximum WPs in flight simultaneously.
        agent_selection: Agent assignment configuration.
        log_dir: Where to write agent execution logs.
        state_file: Where to persist provider-local run state.
    """

    repo_root: Path
    actor: str
    max_concurrent_wps: int = 4
    agent_selection: AgentSelectionConfig = field(
        default_factory=AgentSelectionConfig
    )
    log_dir: Path = field(init=False)
    state_file: Path = field(init=False)

    def __post_init__(self) -> None:
        kittify = self.repo_root / ".kittify"
        self.log_dir = kittify / "logs"
        self.state_file = kittify / "orchestrator-run-state.json"


def load_config(repo_root: Path, actor: str, **overrides: Any) -> OrchestratorConfig:
    """Load OrchestratorConfig, applying any CLI overrides.

    Tries to read `.kittify/orchestrator.yaml` for defaults.
    All values can be overridden via kwargs.

    Args:
        repo_root: Project root.
        actor: Actor identity.
        **overrides: Any OrchestratorConfig field overrides.

    Returns:
        Fully resolved OrchestratorConfig.
    """
    yaml_path = repo_root / ".kittify" / "orchestrator.yaml"
    yaml_data: dict[str, Any] = {}

    if yaml_path.exists():
        try:
            import tomllib  # stdlib in 3.11+

            with open(yaml_path, "rb") as fh:
                yaml_data = tomllib.load(fh)
        except ImportError:
            pass
        except Exception:
            pass

    agent_cfg_data: dict[str, Any] = yaml_data.get("agents", {})
    agent_selection = AgentSelectionConfig(
        implementation_agents=agent_cfg_data.get("implementation", ["claude-code"]),
        review_agents=agent_cfg_data.get("review", ["claude-code"]),
        max_retries=int(agent_cfg_data.get("max_retries", 2)),
        timeout_seconds=int(agent_cfg_data.get("timeout_seconds", 3600)),
        single_agent_mode=bool(agent_cfg_data.get("single_agent_mode", False)),
    )

    cfg = OrchestratorConfig(
        repo_root=repo_root,
        actor=actor,
        max_concurrent_wps=int(
            overrides.get("max_concurrent_wps", yaml_data.get("max_concurrent_wps", 4))
        ),
        agent_selection=agent_selection,
    )

    # Apply overrides for agent selection if provided
    if "implementation_agents" in overrides:
        cfg.agent_selection.implementation_agents = overrides["implementation_agents"]
    if "review_agents" in overrides:
        cfg.agent_selection.review_agents = overrides["review_agents"]

    return cfg


__all__ = [
    "FallbackStrategy",
    "AgentSelectionConfig",
    "OrchestratorConfig",
    "load_config",
]
