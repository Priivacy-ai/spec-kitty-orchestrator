"""Orchestrator configuration loaded from repo config files or CLI flags.

Reads from `.kittify/orchestrator.yaml` / `.kittify/orchestrator.yml` (preferred)
or the legacy `.kittify/orchestrator.toml`, then applies any CLI overrides.
Uses only stdlib + pydantic; no host-internal packages.

Requires Python 3.11+ (tomllib is stdlib) or 'tomli' installed for Python 3.10.
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


def _parse_scalar(value: str) -> Any:
    """Parse a narrow YAML/TOML-like scalar into a Python value."""
    stripped = value.strip()
    if not stripped:
        return ""

    lower = stripped.lower()
    if lower == "true":
        return True
    if lower == "false":
        return False
    if lower in {"null", "none"}:
        return None
    if stripped[:1] == stripped[-1:] and stripped[:1] in {'"', "'"}:
        return stripped[1:-1]
    if stripped.lstrip("-").isdigit():
        return int(stripped)
    return stripped


def _load_yaml_config(path: Path) -> dict[str, Any]:
    """Parse the supported orchestrator YAML subset without extra dependencies."""
    data: dict[str, Any] = {}
    current_section: str | None = None
    current_nested_key: str | None = None

    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue

        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()

        if indent == 0:
            current_nested_key = None
            if stripped.endswith(":"):
                current_section = stripped[:-1].strip()
                data.setdefault(current_section, {})
                continue

            key, sep, value = stripped.partition(":")
            if not sep:
                raise RuntimeError(f"Failed to parse {path}: invalid YAML line {line_number}")
            data[key.strip()] = _parse_scalar(value)
            current_section = None
            continue

        if current_section is None:
            raise RuntimeError(
                f"Failed to parse {path}: unexpected indentation on line {line_number}"
            )

        if indent == 2:
            key, sep, value = stripped.partition(":")
            if not sep:
                raise RuntimeError(f"Failed to parse {path}: invalid YAML line {line_number}")
            section = data.setdefault(current_section, {})
            if not isinstance(section, dict):
                raise RuntimeError(
                    f"Failed to parse {path}: section '{current_section}' must be a mapping"
                )
            if value.strip():
                section[key.strip()] = _parse_scalar(value)
                current_nested_key = None
            else:
                current_nested_key = key.strip()
                section[current_nested_key] = []
            continue

        if indent == 4 and stripped.startswith("- "):
            if current_nested_key is None:
                raise RuntimeError(
                    f"Failed to parse {path}: list item without a parent key on line {line_number}"
                )
            section = data.get(current_section)
            if not isinstance(section, dict):
                raise RuntimeError(
                    f"Failed to parse {path}: section '{current_section}' must be a mapping"
                )
            values = section.get(current_nested_key)
            if not isinstance(values, list):
                raise RuntimeError(
                    f"Failed to parse {path}: key '{current_nested_key}' must be a list"
                )
            values.append(_parse_scalar(stripped[2:]))
            continue

        raise RuntimeError(
            f"Failed to parse {path}: unsupported YAML structure on line {line_number}"
        )

    return data


def _load_toml_config(path: Path) -> dict[str, Any]:
    """Load the legacy TOML configuration file."""
    try:
        import tomllib  # stdlib in Python 3.11+
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError as exc:
            raise RuntimeError(
                f"Cannot parse {path}: Python 3.11+ required for tomllib, "
                "or install 'tomli' for Python 3.10 support."
            ) from exc

    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except Exception as exc:
        raise RuntimeError(f"Failed to parse {path}: {exc}") from exc


def _load_repo_config(repo_root: Path) -> dict[str, Any]:
    """Load orchestrator config from YAML first, then legacy TOML."""
    kittify = repo_root / ".kittify"
    for path in (
        kittify / "orchestrator.yaml",
        kittify / "orchestrator.yml",
    ):
        if path.exists():
            return _load_yaml_config(path)

    toml_path = kittify / "orchestrator.toml"
    if toml_path.exists():
        return _load_toml_config(toml_path)

    return {}


def load_config(repo_root: Path, actor: str, **overrides: Any) -> OrchestratorConfig:
    """Load OrchestratorConfig, applying any CLI overrides.

    Tries `.kittify/orchestrator.yaml` / `.yml` first, then the legacy
    `.kittify/orchestrator.toml`.
    All values can be overridden via kwargs.

    Args:
        repo_root: Project root.
        actor: Actor identity.
        **overrides: Any OrchestratorConfig field overrides.

    Returns:
        Fully resolved OrchestratorConfig.

    Raises:
        RuntimeError: If the repo config exists but cannot be parsed.
    """
    raw_config = _load_repo_config(repo_root)
    agent_cfg_data: dict[str, Any] = raw_config.get("agents", {})
    agent_selection = AgentSelectionConfig(
        implementation_agents=agent_cfg_data.get("implementation", ["claude-code"]),
        review_agents=agent_cfg_data.get("review", ["claude-code"]),
        max_retries=int(raw_config.get("max_retries", agent_cfg_data.get("max_retries", 2))),
        timeout_seconds=int(raw_config.get("timeout_seconds", agent_cfg_data.get("timeout_seconds", 3600))),
        single_agent_mode=bool(raw_config.get("single_agent_mode", agent_cfg_data.get("single_agent_mode", False))),
    )

    cfg = OrchestratorConfig(
        repo_root=repo_root,
        actor=actor,
        max_concurrent_wps=int(
            overrides.get("max_concurrent_wps", raw_config.get("max_concurrent_wps", 4))
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
