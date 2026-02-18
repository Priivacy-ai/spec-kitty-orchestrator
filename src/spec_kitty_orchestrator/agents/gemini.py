"""Google Gemini agent invoker."""

from __future__ import annotations

from pathlib import Path

from .base import BaseInvoker, InvocationResult

GEMINI_EXIT_SUCCESS = 0
GEMINI_EXIT_AUTH_ERROR = 41
GEMINI_EXIT_RATE_LIMIT = 42
GEMINI_EXIT_GENERAL_ERROR = 52
GEMINI_EXIT_INTERRUPTED = 130


class GeminiInvoker(BaseInvoker):
    """Invoker for Google Gemini CLI (gemini).

    Accepts prompts via stdin with -p flag for headless mode.
    Has specific exit codes for different error types.
    """

    agent_id = "gemini"
    command = "gemini"
    uses_stdin = True

    def build_command(self, prompt: str, working_dir: Path, role: str) -> list[str]:
        return [
            "gemini",
            "-p",
            "--yolo",
            "--output-format", "json",
        ]

    def parse_output(
        self, stdout: str, stderr: str, exit_code: int, duration_seconds: float
    ) -> InvocationResult:
        success = exit_code == GEMINI_EXIT_SUCCESS
        data = self._parse_json_output(stdout)
        errors: list[str] = []
        if exit_code == GEMINI_EXIT_AUTH_ERROR:
            errors.append("Gemini authentication error (exit 41)")
        elif exit_code == GEMINI_EXIT_RATE_LIMIT:
            errors.append("Gemini rate limit exceeded (exit 42)")
        elif exit_code == GEMINI_EXIT_GENERAL_ERROR:
            errors.append(f"Gemini general error (exit {exit_code})")
        elif not success:
            errors = self._extract_errors_from_output(data, stderr)
        return InvocationResult(
            success=success,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=duration_seconds,
            files_modified=self._extract_files_from_output(data),
            commits_made=self._extract_commits_from_output(data),
            errors=errors,
            warnings=self._extract_warnings_from_output(data, stderr),
        )
