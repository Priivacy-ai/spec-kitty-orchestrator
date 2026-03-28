"""Qwen Code agent invoker (fork of Gemini CLI)."""

from __future__ import annotations

from pathlib import Path

from .base import BaseInvoker, InvocationResult


class QwenInvoker(BaseInvoker):
    """Invoker for Qwen Code CLI (qwen).

    Fork of Gemini CLI with identical flags.
    Accepts prompts via stdin with -p flag.
    """

    agent_id = "qwen"
    command = "qwen"
    uses_stdin = True

    _IMPLEMENTATION_SYSTEM_PROMPT = (
        "You are operating in implementation mode. Make the requested changes in the "
        "current workspace and leave the work commit-ready."
    )
    _REVIEW_SYSTEM_PROMPT = (
        "You are operating in review mode. Review the existing workspace state only. "
        "Do not modify files, write code, or produce an implementation summary. "
        "Approve only when the current workspace satisfies the stated requirements; "
        "otherwise return concrete defects with exact file paths."
    )

    def build_command(self, prompt: str, working_dir: Path, role: str) -> list[str]:
        cmd = [
            "qwen",
            "-p",
            "--yolo",
            "--output-format", "json",
        ]
        if role == "review":
            cmd.extend(["--append-system-prompt", self._REVIEW_SYSTEM_PROMPT])
        else:
            cmd.extend(["--append-system-prompt", self._IMPLEMENTATION_SYSTEM_PROMPT])
        return cmd

    def parse_output(
        self, stdout: str, stderr: str, exit_code: int, duration_seconds: float
    ) -> InvocationResult:
        success = exit_code == 0
        data = self._parse_json_output(stdout)
        return InvocationResult(
            success=success,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=duration_seconds,
            files_modified=self._extract_files_from_output(data),
            commits_made=self._extract_commits_from_output(data),
            errors=self._extract_errors_from_output(data, stderr),
            warnings=self._extract_warnings_from_output(data, stderr),
        )
