"""Augment Code agent invoker (auggie)."""

from __future__ import annotations

from pathlib import Path

from .base import BaseInvoker, InvocationResult


class AugmentInvoker(BaseInvoker):
    """Invoker for Augment Code CLI (auggie).

    Uses --acp for autonomous coding prompt mode.
    Does not support JSON output — relies on exit code only.
    """

    agent_id = "augment"
    command = "auggie"
    uses_stdin = False

    def build_command(self, prompt: str, working_dir: Path, role: str) -> list[str]:
        return [
            "auggie",
            "--acp",
            prompt,
        ]

    def parse_output(
        self, stdout: str, stderr: str, exit_code: int, duration_seconds: float
    ) -> InvocationResult:
        """Auggie has no JSON output; rely on exit code."""
        success = exit_code == 0
        errors: list[str] = []
        if not success:
            errors = self._extract_errors_from_output(None, stderr)
            if not errors and stderr.strip():
                errors = [stderr.strip()[:200]]
        return InvocationResult(
            success=success,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=duration_seconds,
            files_modified=[],
            commits_made=[],
            errors=errors,
            warnings=[],
        )
