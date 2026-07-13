"""Tests for incremental subtask tracking helpers (issue #22)."""

from __future__ import annotations

from spec_kitty_orchestrator.loop import (
    _extract_subtask_ids,
    _inject_subtask_tracking,
)


def _prompt(frontmatter: str, body: str = "Body") -> str:
    return f"---\n{frontmatter}\n---\n\n{body}"


# ---------------------------------------------------------------------------
# _extract_subtask_ids
# ---------------------------------------------------------------------------

class TestExtractSubtaskIds:
    def test_standard_frontmatter(self) -> None:
        prompt = _prompt("wp_id: WP01\nsubtasks:\n- T001\n- T002\n- T003")
        assert _extract_subtask_ids(prompt) == ["T001", "T002", "T003"]

    def test_indented_items(self) -> None:
        prompt = _prompt("subtasks:\n  - T001\n  - T002")
        assert _extract_subtask_ids(prompt) == ["T001", "T002"]

    def test_canonical_quoted_items_are_normalized(self) -> None:
        prompt = _prompt('subtasks:\n  - "T001"\n  - "T002"')
        assert _extract_subtask_ids(prompt) == ["T001", "T002"]

    def test_inline_list_is_supported(self) -> None:
        prompt = _prompt('subtasks: ["T001", "T002"]')
        assert _extract_subtask_ids(prompt) == ["T001", "T002"]

    def test_legacy_unquoted_inline_list_is_supported(self) -> None:
        prompt = _prompt("subtasks: [T001, T002]")
        assert _extract_subtask_ids(prompt) == ["T001", "T002"]

    def test_no_subtasks_block(self) -> None:
        prompt = _prompt("wp_id: WP01\ntitle: no subtasks here")
        assert _extract_subtask_ids(prompt) == []

    def test_empty_subtasks_block(self) -> None:
        # No items under the header — regex won't match the block
        prompt = _prompt("subtasks:\n\ntitle: empty")
        assert _extract_subtask_ids(prompt) == []

    def test_single_subtask(self) -> None:
        prompt = _prompt("subtasks:\n- T001")
        assert _extract_subtask_ids(prompt) == ["T001"]

    def test_alphanumeric_ids(self) -> None:
        prompt = _prompt("subtasks:\n- TASK-1\n- TASK-2")
        assert _extract_subtask_ids(prompt) == ["TASK-1", "TASK-2"]

    def test_does_not_match_subtasks_in_body(self) -> None:
        # A ``subtasks:`` word in the body (not at line start) should not match
        prompt = _prompt("title: foo", "## Notes\n\nsubtasks:\n- T001")
        assert _extract_subtask_ids(prompt) == []

    def test_invalid_cli_token_is_rejected(self) -> None:
        prompt = _prompt('subtasks:\n- "T001;touch-pwned"\n- T002')
        assert _extract_subtask_ids(prompt) == ["T002"]


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

    def test_mission_slug_is_shell_quoted(self) -> None:
        result = _inject_subtask_tracking("body", ["T001"], "mission;echo unsafe")
        assert "--mission 'mission;echo unsafe'" in result
