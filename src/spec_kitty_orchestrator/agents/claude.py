"""Claude Code agent invoker."""

from __future__ import annotations

from pathlib import Path

from .base import BaseInvoker, InvocationResult


class ClaudeInvoker(BaseInvoker):
    """Invoker for Claude Code CLI (claude).

    Accepts prompts via stdin with -p flag for headless mode.
    Outputs structured JSON with --output-format json.
    """

    agent_id = "claude-code"
    command = "claude"
    uses_stdin = True

    def build_command(self, prompt: str, working_dir: Path, role: str) -> list[str]:
        cmd = [
            "claude",
            "-p",
            "--output-format", "json",
            "--dangerously-skip-permissions",
        ]
        if role == "implementation":
            cmd.extend(["--allowedTools", "Read,Write,Edit,Bash,Glob,Grep,TodoWrite"])
        elif role == "review":
            cmd.extend(["--allowedTools", "Read,Glob,Grep,Bash"])
        return cmd

    def parse_output(
        self, stdout: str, stderr: str, exit_code: int, duration_seconds: float
    ) -> InvocationResult:
        success = exit_code == 0
        data = self._parse_json_output(stdout)
        files_modified: list[str] = []
        commits_made: list[str] = []
        errors: list[str] = []
        warnings: list[str] = []
        if isinstance(data, dict):
            result = data.get("result", data)
            if isinstance(result, dict):
                files_modified = self._extract_files_from_output(result)
                commits_made = self._extract_commits_from_output(result)
            if "error" in data:
                errors.append(str(data["error"]))
        if not errors and stderr.strip():
            errors = self._extract_errors_from_output(None, stderr)
        warnings = self._extract_warnings_from_output(data, stderr)
        return InvocationResult(
            success=success,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=duration_seconds,
            files_modified=files_modified,
            commits_made=commits_made,
            errors=errors,
            warnings=warnings,
        )
