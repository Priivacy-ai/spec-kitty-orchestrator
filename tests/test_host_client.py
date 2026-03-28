from __future__ import annotations

import json
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from spec_kitty_orchestrator.host.client import HostClient


def _envelope(data: dict[str, object]) -> str:
    return json.dumps(
        {
            "contract_version": "1.0.0",
            "command": "contract-version",
            "timestamp": "2026-03-27T00:00:00Z",
            "correlation_id": "test-correlation",
            "success": True,
            "error_code": None,
            "data": data,
        }
    )


def test_orchestrator_api_retries_without_json_flag_when_host_rejects_it() -> None:
    client = HostClient(
        repo_root=Path("/tmp/test-repo"),
        actor="test-orchestrator",
        policy_json='{"orchestrator_id":"test"}',
    )

    first = CompletedProcess(
        args=["spec-kitty", "orchestrator-api", "contract-version", "--json"],
        returncode=2,
        stdout="",
        stderr="No such option: --json",
    )
    second = CompletedProcess(
        args=["spec-kitty", "orchestrator-api", "contract-version"],
        returncode=0,
        stdout=_envelope(
            {
                "api_version": "1.0.0",
                "min_supported_provider_version": "0.1.0",
            }
        ),
        stderr="",
    )

    with patch("spec_kitty_orchestrator.host.client.subprocess.run", side_effect=[first, second]) as run:
        result = client.contract_version()

    assert result.api_version == "1.0.0"
    first_cmd = run.call_args_list[0].args[0]
    second_cmd = run.call_args_list[1].args[0]
    assert first_cmd[-1] == "--json"
    assert "--json" not in second_cmd


def test_orchestrator_api_retries_when_host_reports_json_flag_error_as_envelope() -> None:
    client = HostClient(
        repo_root=Path("/tmp/test-repo"),
        actor="test-orchestrator",
        policy_json='{"orchestrator_id":"test"}',
    )

    first = CompletedProcess(
        args=["spec-kitty", "orchestrator-api", "contract-version", "--json"],
        returncode=2,
        stdout=json.dumps(
            {
                "contract_version": "1.0.0",
                "command": "contract-version",
                "timestamp": "2026-03-27T00:00:00Z",
                "correlation_id": "test-correlation",
                "success": False,
                "error_code": "USAGE_ERROR",
                "data": {"message": "No such option: --json"},
            }
        ),
        stderr="",
    )
    second = CompletedProcess(
        args=["spec-kitty", "orchestrator-api", "contract-version"],
        returncode=0,
        stdout=_envelope(
            {
                "api_version": "1.0.0",
                "min_supported_provider_version": "0.1.0",
            }
        ),
        stderr="",
    )

    with patch("spec_kitty_orchestrator.host.client.subprocess.run", side_effect=[first, second]) as run:
        result = client.contract_version()

    assert result.api_version == "1.0.0"
    first_cmd = run.call_args_list[0].args[0]
    second_cmd = run.call_args_list[1].args[0]
    assert first_cmd[-1] == "--json"
    assert "--json" not in second_cmd


def test_emit_status_transition_passes_evidence_and_handoff_flags() -> None:
    client = HostClient(
        repo_root=Path("/tmp/test-repo"),
        actor="test-orchestrator",
        policy_json='{"orchestrator_id":"test"}',
    )

    result = CompletedProcess(
        args=["spec-kitty", "orchestrator-api", "transition", "--json"],
        returncode=0,
        stdout=_envelope(
            {
                "feature_slug": "010-test-feature",
                "wp_id": "WP01",
                "from_lane": "for_review",
                "to_lane": "done",
                "policy_metadata_recorded": True,
            }
        ),
        stderr="",
    )

    with patch("spec_kitty_orchestrator.host.client.subprocess.run", return_value=result) as run:
        client.emit_status_transition(
            "010-test-feature",
            "WP01",
            "done",
            review_ref="review-123",
            evidence={"review": {"reviewer": "qwen", "verdict": "approved"}},
            subtasks_complete=True,
            implementation_evidence_present=True,
        )

    cmd = run.call_args.args[0]
    assert cmd[:3] == ["spec-kitty", "orchestrator-api", "transition"]
    assert "--review-ref" in cmd
    assert "--evidence-json" in cmd
    assert "--subtasks-complete" in cmd
    assert "--implementation-evidence-present" in cmd
