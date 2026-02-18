"""OpenCode agent invoker."""

from __future__ import annotations

from pathlib import Path

from .base import BaseInvoker, InvocationResult


class OpenCodeInvoker(BaseInvoker):
    """Invoker for OpenCode CLI (opencode).

    Multi-provider agent supporting various LLM backends.
    Uses `opencode run` with stdin for prompts.
    """

    agent_id = "opencode"
    command = "opencode"
    uses_stdin = True

    def build_command(self, prompt: str, working_dir: Path, role: str) -> list[str]:
        return [
            "opencode", "run",
            "--agent", "build",
            "--format", "json",
        ]

    def parse_output(
        self, stdout: str, stderr: str, exit_code: int, duration_seconds: float
    ) -> InvocationResult:
        """Parse OpenCode JSON streaming output (one event per line)."""
        success = exit_code == 0
        # OpenCode uses JSONL; extract meaningful events
        files_modified: list[str] = []
        errors: list[str] = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                import json
                event = json.loads(line)
                if isinstance(event, dict):
                    if event.get("type") == "file_write":
                        path = event.get("path", "")
                        if path:
                            files_modified.append(path)
                    if event.get("type") == "error":
                        errors.append(str(event.get("message", "")))
            except Exception:
                pass
        if not success and not errors:
            errors = self._extract_errors_from_output(None, stderr)
        return InvocationResult(
            success=success,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=duration_seconds,
            files_modified=files_modified,
            commits_made=[],
            errors=errors,
            warnings=[],
        )
