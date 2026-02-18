"""GitHub Copilot agent invoker."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .base import BaseInvoker, InvocationResult


class CopilotInvoker(BaseInvoker):
    """Invoker for GitHub Copilot CLI (gh copilot).

    Prompt passed as -p argument (not stdin). Uses --yolo for autonomous mode.
    Requires gh CLI with copilot extension installed.
    """

    agent_id = "copilot"
    command = "gh"
    uses_stdin = False

    def is_installed(self) -> bool:
        if not shutil.which("gh"):
            return False
        try:
            result = subprocess.run(
                ["gh", "extension", "list"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
            return "copilot" in result.stdout.lower()
        except (subprocess.TimeoutExpired, subprocess.SubprocessError):
            return False

    def build_command(self, prompt: str, working_dir: Path, role: str) -> list[str]:
        return [
            "gh", "copilot", "suggest",
            "-t", "shell",
            "--yolo",
            "-p", prompt,
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
