"""Cursor agent invoker with mandatory timeout wrapper."""

from __future__ import annotations

import shutil
from pathlib import Path

from .base import BaseInvoker, InvocationResult

TIMEOUT_EXIT_CODE = 124


class CursorInvoker(BaseInvoker):
    """Invoker for Cursor CLI (cursor) with mandatory timeout wrapper.

    IMPORTANT: Cursor CLI may hang indefinitely, so we ALWAYS wrap it
    with the Unix `timeout` command to ensure eventual termination.
    """

    agent_id = "cursor"
    command = "cursor"
    uses_stdin = False
    default_timeout = 300  # 5 minutes

    def __init__(self, timeout_seconds: int | None = None) -> None:
        if timeout_seconds is not None and timeout_seconds > 0:
            self.timeout = timeout_seconds
        else:
            self.timeout = self.default_timeout

    def is_installed(self) -> bool:
        return (
            shutil.which(self.command) is not None
            and shutil.which("timeout") is not None
        )

    def build_command(self, prompt: str, working_dir: Path, role: str) -> list[str]:
        return [
            "timeout", str(self.timeout),
            "cursor", "agent",
            "-p", prompt,
        ]

    def parse_output(
        self, stdout: str, stderr: str, exit_code: int, duration_seconds: float
    ) -> InvocationResult:
        if exit_code == TIMEOUT_EXIT_CODE:
            return InvocationResult(
                success=False,
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                duration_seconds=duration_seconds,
                errors=[f"Cursor timed out after {self.timeout}s"],
            )
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
