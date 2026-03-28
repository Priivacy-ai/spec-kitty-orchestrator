from __future__ import annotations

from pathlib import Path

from spec_kitty_orchestrator.agents.qwen import QwenInvoker


def test_qwen_review_command_includes_review_system_prompt() -> None:
    invoker = QwenInvoker()

    cmd = invoker.build_command("prompt", Path("/tmp/workspace"), role="review")

    assert "--append-system-prompt" in cmd
    system_prompt = cmd[cmd.index("--append-system-prompt") + 1]
    assert "review mode" in system_prompt
    assert "Do not modify files" in system_prompt


def test_qwen_implementation_command_includes_implementation_system_prompt() -> None:
    invoker = QwenInvoker()

    cmd = invoker.build_command("prompt", Path("/tmp/workspace"), role="implementation")

    assert "--append-system-prompt" in cmd
    system_prompt = cmd[cmd.index("--append-system-prompt") + 1]
    assert "implementation mode" in system_prompt
    assert "commit-ready" in system_prompt
