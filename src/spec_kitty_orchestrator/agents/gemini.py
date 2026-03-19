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

    Current Gemini CLI requires a non-empty `--prompt` value for headless mode,
    but also appends any stdin content. We send a benign inline prompt marker
    and stream the full task prompt over stdin so large prompts and YAML
    frontmatter remain intact.
    Has specific exit codes for different error types.
    """

    agent_id = "gemini"
    command = "gemini"
    uses_stdin = True

    def __init__(self, model: str = "auto") -> None:
        self.model = model

    def build_command(self, prompt: str, working_dir: Path, role: str) -> list[str]:
        return [
            "gemini",
            "--model",
            self.model,
            "--prompt",
            "stdin prompt follows",
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

    def detect_runtime_termination(
        self,
        stdout: str,
        stderr: str,
    ) -> tuple[int, str] | None:
        """Terminate early when Gemini emits fatal provider errors to stderr."""
        stderr_lower = stderr.lower()

        auth_markers = (
            "authentication failed",
            "invalid_grant",
            "unauthorized",
            "permission denied",
        )
        if any(marker in stderr_lower for marker in auth_markers):
            return (GEMINI_EXIT_AUTH_ERROR, "Gemini authentication failure detected from stderr")

        rate_limit_markers = (
            "you have exhausted your capacity on this model",
            "no capacity available for model",
            "model_capacity_exhausted",
            "resource_exhausted",
            "ratelimitexceeded",
            "status: 429",
            '"code": 429',
        )
        if any(marker in stderr_lower for marker in rate_limit_markers):
            return (GEMINI_EXIT_RATE_LIMIT, "Gemini rate limit/capacity exhaustion detected from stderr")

        return None
