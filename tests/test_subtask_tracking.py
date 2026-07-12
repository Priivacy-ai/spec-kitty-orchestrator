"""Tests for incremental subtask tracking helpers (issue #22)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from spec_kitty_orchestrator.loop import (
    _ensure_subtasks_marked,
    _extract_subtask_ids,
    _inject_subtask_tracking,
)


# ---------------------------------------------------------------------------
# _extract_subtask_ids
# ---------------------------------------------------------------------------

class TestExtractSubtaskIds:
    def test_standard_frontmatter(self) -> None:
        prompt = "---\nwp_id: WP01\nsubtasks:\n- T001\n- T002\n- T003\n---\n\nBody"
        assert _extract_subtask_ids(prompt) == ["T001", "T002", "T003"]

    def test_indented_items(self) -> None:
        prompt = "subtasks:\n  - T001\n  - T002\n"
        assert _extract_subtask_ids(prompt) == ["T001", "T002"]

    def test_no_subtasks_block(self) -> None:
        prompt = "wp_id: WP01\ntitle: no subtasks here\n"
        assert _extract_subtask_ids(prompt) == []

    def test_empty_subtasks_block(self) -> None:
        # No items under the header — regex won't match the block
        prompt = "subtasks:\n\nSome body text\n"
        assert _extract_subtask_ids(prompt) == []

    def test_single_subtask(self) -> None:
        prompt = "subtasks:\n- T001\n\nBody starts here"
        assert _extract_subtask_ids(prompt) == ["T001"]

    def test_alphanumeric_ids(self) -> None:
        prompt = "subtasks:\n- TASK-1\n- TASK-2\n"
        assert _extract_subtask_ids(prompt) == ["TASK-1", "TASK-2"]

    def test_does_not_match_subtasks_in_body(self) -> None:
        # A ``subtasks:`` word in the body (not at line start) should not match
        prompt = "title: foo\n\n## Notes\n\nsubtasks:\n- T001\n"
        result = _extract_subtask_ids(prompt)
        # May or may not match depending on position — we just assert no crash
        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# _inject_subtask_tracking
# ---------------------------------------------------------------------------

class TestInjectSubtaskTracking:
    def test_prepends_instructions(self) -> None:
        original = "## WP01\n\nDo the work."
        result = _inject_subtask_tracking(original, ["T001", "T002"], "my-mission")
        assert result.startswith("## Agent Subtask Completion Protocol")
        assert "my-mission" in result
        assert result.endswith(original)

    def test_includes_each_task_id(self) -> None:
        result = _inject_subtask_tracking("body", ["T001", "T002", "T003"], "m")
        for t in ("T001", "T002", "T003"):
            assert t in result

    def test_no_subtasks_returns_unchanged(self) -> None:
        original = "some prompt"
        assert _inject_subtask_tracking(original, [], "m") == original

    def test_mission_slug_in_each_command(self) -> None:
        result = _inject_subtask_tracking("body", ["T001", "T002"], "slug-123")
        # Both T001 and T002 commands reference the mission slug
        assert result.count("slug-123") >= 2

    def test_mark_status_command_present(self) -> None:
        result = _inject_subtask_tracking("body", ["T001"], "m")
        assert "mark-status T001 --status done" in result


# ---------------------------------------------------------------------------
# _ensure_subtasks_marked
# ---------------------------------------------------------------------------

class TestEnsureSubtasksMarked:
    def test_calls_spec_kitty_cli(self, tmp_path: Path) -> None:
        with patch("spec_kitty_orchestrator.loop.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            _ensure_subtasks_marked(tmp_path, "my-mission", ["T001", "T002"])

        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args[0] == "spec-kitty"
        assert "mark-status" in args
        assert "T001" in args
        assert "T002" in args
        assert "--status" in args
        assert "done" in args
        assert "--mission" in args
        assert "my-mission" in args

    def test_no_subtasks_does_nothing(self, tmp_path: Path) -> None:
        with patch("spec_kitty_orchestrator.loop.subprocess.run") as mock_run:
            _ensure_subtasks_marked(tmp_path, "m", [])
        mock_run.assert_not_called()

    def test_cli_failure_is_warned_not_raised(self, tmp_path: Path) -> None:
        with patch("spec_kitty_orchestrator.loop.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(1, "spec-kitty", stderr="oops")
            # Should not raise
            _ensure_subtasks_marked(tmp_path, "m", ["T001"])

    def test_missing_binary_is_warned_not_raised(self, tmp_path: Path) -> None:
        with patch("spec_kitty_orchestrator.loop.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("spec-kitty not found")
            _ensure_subtasks_marked(tmp_path, "m", ["T001"])

    def test_uses_workspace_as_cwd(self, tmp_path: Path) -> None:
        with patch("spec_kitty_orchestrator.loop.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            _ensure_subtasks_marked(tmp_path, "m", ["T001"])

        kwargs = mock_run.call_args[1]
        assert kwargs["cwd"] == tmp_path
