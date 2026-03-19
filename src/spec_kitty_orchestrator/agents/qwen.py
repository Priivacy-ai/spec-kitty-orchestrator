"""Qwen Code agent invoker (fork of Gemini CLI)."""

from __future__ import annotations

from pathlib import Path

from .base import BaseInvoker, InvocationResult


class QwenInvoker(BaseInvoker):
    """Invoker for Qwen Code CLI (qwen).

    Fork of Gemini CLI with identical flags.
    Current CLI expects the prompt as the `-p/--prompt` value for headless mode.
    """

    agent_id = "qwen"
    command = "qwen"
    uses_stdin = False

    def build_command(self, prompt: str, working_dir: Path, role: str) -> list[str]:
        return [
            "qwen",
            "-p",
            prompt,
            "--yolo",
            "--output-format", "json",
        ]

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

    def detect_runtime_termination(
        self,
        stdout: str,
        stderr: str,
    ) -> tuple[int, str] | None:
        """Qwen CLI follows Gemini stderr patterns for provider failures."""
        stderr_lower = stderr.lower()
        if any(marker in stderr_lower for marker in ("resource_exhausted", "ratelimitexceeded", "no capacity available for model")):
            return (42, "Qwen provider rate limit/capacity exhaustion detected from stderr")
        if any(marker in stderr_lower for marker in ("authentication failed", "invalid_grant", "unauthorized")):
            return (41, "Qwen authentication failure detected from stderr")
        return None
