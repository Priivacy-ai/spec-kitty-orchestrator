from __future__ import annotations

from pathlib import Path

from spec_kitty_orchestrator.cli.main import _build_policy
from spec_kitty_orchestrator.config import AgentSelectionConfig, OrchestratorConfig


def test_build_policy_uses_primary_qwen_agent_family() -> None:
    cfg = OrchestratorConfig(
        repo_root=Path("/tmp/test-repo"),
        actor="test-orchestrator",
        agent_selection=AgentSelectionConfig(
            implementation_agents=["qwen"],
            review_agents=["qwen"],
        ),
    )

    policy = _build_policy(cfg)

    assert policy.agent_family == "qwen"
    assert policy.network_mode == "open"
    assert policy.dangerous_flags == ["--yolo"]
