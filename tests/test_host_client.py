from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from spec_kitty_orchestrator.host.client import HostClient


def test_host_client_retries_read_calls_without_json_flag(tmp_path: Path) -> None:
    client = HostClient(tmp_path, actor="spec-kitty-orchestrator", policy_json="{}")
    calls: list[list[str]] = []

    def _fake_run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if cmd[-1] == "--json":
            return subprocess.CompletedProcess(
                cmd,
                2,
                stdout="",
                stderr="No such option: --json\n",
            )
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=(
                '{"contract_version":"1.0.0","command":"orchestrator-api.list-ready",'
                '"timestamp":"2026-03-19T22:28:18.681802+00:00","correlation_id":"corr-test",'
                '"success":true,"error_code":null,'
                '"data":{"feature_slug":"004-aegis-live-runtime-mvp","ready_work_packages":[]}}'
            ),
            stderr="",
        )

    with patch("spec_kitty_orchestrator.host.client.subprocess.run", side_effect=_fake_run):
        result = client.list_ready("004-aegis-live-runtime-mvp")

    assert result.feature_slug == "004-aegis-live-runtime-mvp"
    assert result.ready_work_packages == []
    assert calls == [
        [
            "spec-kitty",
            "orchestrator-api",
            "list-ready",
            "--feature",
            "004-aegis-live-runtime-mvp",
            "--json",
        ],
        [
            "spec-kitty",
            "orchestrator-api",
            "list-ready",
            "--feature",
            "004-aegis-live-runtime-mvp",
        ],
    ]
