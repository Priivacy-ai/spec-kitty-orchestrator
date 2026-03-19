from __future__ import annotations

from pathlib import Path

from spec_kitty_orchestrator.agents.gemini import GeminiInvoker
from spec_kitty_orchestrator.agents.qwen import QwenInvoker


def test_gemini_invoker_passes_prompt_as_flag_argument() -> None:
    invoker = GeminiInvoker()

    cmd = invoker.build_command("review this work package", Path("."), "review")

    assert invoker.uses_stdin is False
    assert cmd == [
        "gemini",
        "-p",
        "review this work package",
        "--yolo",
        "--output-format",
        "json",
    ]


def test_qwen_invoker_passes_prompt_as_flag_argument() -> None:
    invoker = QwenInvoker()

    cmd = invoker.build_command("implement this work package", Path("."), "implementation")

    assert invoker.uses_stdin is False
    assert cmd == [
        "qwen",
        "-p",
        "implement this work package",
        "--yolo",
        "--output-format",
        "json",
    ]
