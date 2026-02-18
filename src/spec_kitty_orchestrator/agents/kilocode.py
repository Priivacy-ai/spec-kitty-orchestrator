"""Kilocode agent invoker."""

from __future__ import annotations

from pathlib import Path

from .base import BaseInvoker, InvocationResult


class KilocodeInvoker(BaseInvoker):
    """Invoker for Kilocode CLI (kilocode).

    Prompt passed as positional argument. Uses -a for autonomous mode
    and -j for JSON output.
    """

    agent_id = "kilocode"
    command = "kilocode"
    uses_stdin = False

    def build_command(self, prompt: str, working_dir: Path, role: str) -> list[str]:
        return [
            "kilocode",
            "-a",
            "--yolo",
            "-j",
            prompt,
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
