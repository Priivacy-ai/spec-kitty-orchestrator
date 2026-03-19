from __future__ import annotations

from pathlib import Path

from spec_kitty_orchestrator.agents.gemini import GeminiInvoker
from spec_kitty_orchestrator.agents.qwen import QwenInvoker


def test_gemini_invoker_passes_prompt_as_flag_argument() -> None:
    invoker = GeminiInvoker()

    cmd = invoker.build_command("review this work package", Path("."), "review")

    assert invoker.uses_stdin is True
    assert cmd == [
        "gemini",
        "--model",
        "auto",
        "--prompt",
        "stdin prompt follows",
        "--yolo",
        "--output-format",
        "json",
    ]


def test_qwen_invoker_passes_prompt_as_flag_argument() -> None:
    invoker = QwenInvoker()

    cmd = invoker.build_command("implement this work package", Path("."), "implementation")

    assert invoker.uses_stdin is True
    assert cmd == [
        "qwen",
        "--prompt",
        "stdin prompt follows",
        "--yolo",
        "--output-format",
        "json",
    ]


def test_get_invoker_supports_explicit_gemini_model_alias() -> None:
    from spec_kitty_orchestrator.agents import get_invoker

    invoker = get_invoker("gemini-2.5-flash-lite")

    assert isinstance(invoker, GeminiInvoker)
    assert invoker.model == "gemini-2.5-flash-lite"
