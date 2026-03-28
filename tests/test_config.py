from __future__ import annotations

from pathlib import Path

from spec_kitty_orchestrator.config import load_config


def test_load_config_reads_yaml_configuration(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    kittify = repo_root / ".kittify"
    kittify.mkdir(parents=True)
    (kittify / "orchestrator.yaml").write_text(
        "\n".join(
            [
                "agents:",
                "  implementation:",
                "    - qwen",
                "  review:",
                "    - qwen",
                "max_concurrent_wps: 1",
                "max_retries: 3",
                "timeout_seconds: 3600",
                "single_agent_mode: false",
            ]
        ),
        encoding="utf-8",
    )

    cfg = load_config(repo_root, actor="spec-kitty-orchestrator")

    assert cfg.agent_selection.implementation_agents == ["qwen"]
    assert cfg.agent_selection.review_agents == ["qwen"]
    assert cfg.max_concurrent_wps == 1
    assert cfg.agent_selection.max_retries == 3
    assert cfg.agent_selection.timeout_seconds == 3600
    assert cfg.agent_selection.single_agent_mode is False


def test_load_config_keeps_legacy_toml_support(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    kittify = repo_root / ".kittify"
    kittify.mkdir(parents=True)
    (kittify / "orchestrator.toml").write_text(
        "\n".join(
            [
                "max_concurrent_wps = 2",
                "max_retries = 4",
                "timeout_seconds = 1800",
                "",
                "[agents]",
                'implementation = ["gemini"]',
                'review = ["codex"]',
            ]
        ),
        encoding="utf-8",
    )

    cfg = load_config(repo_root, actor="spec-kitty-orchestrator")

    assert cfg.agent_selection.implementation_agents == ["gemini"]
    assert cfg.agent_selection.review_agents == ["codex"]
    assert cfg.max_concurrent_wps == 2
    assert cfg.agent_selection.max_retries == 4
    assert cfg.agent_selection.timeout_seconds == 1800
