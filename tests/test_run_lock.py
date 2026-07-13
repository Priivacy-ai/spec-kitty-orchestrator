from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from spec_kitty_orchestrator.run_lock import (
    OrchestrationAlreadyRunningError,
    orchestration_lock,
)


def test_lock_rejects_concurrent_live_owner(tmp_path: Path) -> None:
    lock_file = tmp_path / "orchestrator.lock"
    with orchestration_lock(lock_file, "mission-a"):
        with pytest.raises(OrchestrationAlreadyRunningError, match="mission-a"):
            with orchestration_lock(lock_file, "mission-b"):
                pytest.fail("concurrent lock unexpectedly acquired")


def test_lock_recovers_dead_owner_and_cleans_up(tmp_path: Path) -> None:
    lock_file = tmp_path / "orchestrator.lock"
    lock_file.write_text(
        json.dumps({"pid": 999_999_999, "mission": "stale", "token": "old"}),
        encoding="utf-8",
    )

    with orchestration_lock(lock_file, "mission-new"):
        owner = json.loads(lock_file.read_text(encoding="utf-8"))
        assert owner["pid"] == os.getpid()
        assert owner["mission"] == "mission-new"

    assert not lock_file.exists()
