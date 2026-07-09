from __future__ import annotations

import json

import pytest

from spec_kitty_orchestrator.host.client import HostClient

from .harness import run_command


pytestmark = pytest.mark.e2e


def test_spec_kitty_orchestrator_api_is_json_without_json_flag(fake_agent_project) -> None:
    result = run_command(
        ["spec-kitty", "orchestrator-api", "contract-version"],
        cwd=fake_agent_project.root,
        env=fake_agent_project.env,
    )

    assert result.returncode == 0, result.combined
    payload = json.loads(result.stdout)
    assert payload["success"] is True
    assert payload["command"] == "orchestrator-api.contract-version"


def test_host_client_contract_version_works_against_real_spec_kitty(fake_agent_project) -> None:
    client = HostClient(
        repo_root=fake_agent_project.root,
        actor="e2e",
        policy_json=None,
        bin_path=str(fake_agent_project.bin_dir / "spec-kitty"),
    )

    data = client.contract_version()

    # Host contract is 1.2.0 (adds read-only resolve-workspace). The handshake
    # succeeds because the orchestrator's _MIN_CONTRACT_VERSION is also 1.2.0.
    assert data.api_version == "1.2.0"


def test_host_client_can_query_seeded_ready_wp(fake_agent_project) -> None:
    client = HostClient(
        repo_root=fake_agent_project.root,
        actor="e2e",
        policy_json=None,
        bin_path=str(fake_agent_project.bin_dir / "spec-kitty"),
    )

    state = client.mission_state(fake_agent_project.mission_slug)
    ready = client.list_ready(fake_agent_project.mission_slug)

    assert [wp.wp_id for wp in state.work_packages] == ["WP01"]
    assert [wp.wp_id for wp in ready.ready_work_packages] == ["WP01"]
