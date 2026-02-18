"""Base protocol and abstract class for agent invokers.

Defines:
- AgentInvoker Protocol for static type checking
- InvocationResult dataclass capturing execution results
- BaseInvoker abstract base with common JSON-parsing helpers
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass
class InvocationResult:
    """Result of a single agent invocation.

    Captures all relevant output from one agent execution, including success
    status, captured output, and any structured data extracted from JSON output.
    """

    success: bool
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    files_modified: list[str] = field(default_factory=list)
    commits_made: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@runtime_checkable
class AgentInvoker(Protocol):
    """Protocol defining the interface for all agent invokers."""

    agent_id: str
    command: str
    uses_stdin: bool

    def is_installed(self) -> bool:
        """Return True if the agent CLI is available on PATH."""
        ...

    def build_command(
        self,
        prompt: str,
        working_dir: Path,
        role: str,
    ) -> list[str]:
        """Build the full subprocess command list.

        Args:
            prompt: Task prompt (may be sent via stdin or as argument).
            working_dir: Directory where agent should run.
            role: "implementation" or "review".

        Returns:
            List of strings for subprocess.run / asyncio.create_subprocess_exec.
        """
        ...

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        duration_seconds: float,
    ) -> InvocationResult:
        """Parse raw subprocess output into a structured InvocationResult."""
        ...


class BaseInvoker:
    """Abstract base class implementing common agent invoker helpers."""

    agent_id: str = ""
    command: str = ""
    uses_stdin: bool = True

    def is_installed(self) -> bool:
        """Return True if the agent CLI binary is on PATH."""
        return shutil.which(self.command) is not None

    def build_command(
        self,
        prompt: str,
        working_dir: Path,
        role: str,
    ) -> list[str]:
        """Subclasses must override to return the agent command."""
        raise NotImplementedError

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        duration_seconds: float,
    ) -> InvocationResult:
        """Default output parser; uses JSON if available, falls back to text."""
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

    # ── JSON helpers ─────────────────────────────────────────────────────────

    def _parse_json_output(self, stdout: str) -> dict | list | None:
        """Try to parse stdout as JSON (object or array)."""
        if not stdout.strip():
            return None
        try:
            return json.loads(stdout)
        except json.JSONDecodeError:
            pass
        # Try last non-empty line (JSONL format)
        for line in reversed(stdout.strip().splitlines()):
            line = line.strip()
            if line.startswith(("{", "[")):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
        return None

    def _extract_files_from_output(self, data: dict | list | None) -> list[str]:
        if not isinstance(data, dict):
            return []
        for key in ("files", "files_modified", "modified_files", "changedFiles"):
            if key in data and isinstance(data[key], list):
                return [str(f) for f in data[key]]
        return []

    def _extract_commits_from_output(self, data: dict | list | None) -> list[str]:
        if not isinstance(data, dict):
            return []
        for key in ("commits", "commits_made", "commitShas"):
            if key in data and isinstance(data[key], list):
                return [str(c) for c in data[key]]
        return []

    def _extract_errors_from_output(
        self, data: dict | list | None, stderr: str
    ) -> list[str]:
        errors: list[str] = []
        if isinstance(data, dict):
            for key in ("errors", "error"):
                if key in data:
                    val = data[key]
                    if isinstance(val, list):
                        errors.extend(str(e) for e in val)
                    elif val:
                        errors.append(str(val))
        if stderr.strip():
            stderr_lines = [
                line.strip()
                for line in stderr.splitlines()
                if line.strip() and not line.lower().startswith("warning")
            ]
            if any("error" in line.lower() for line in stderr_lines):
                errors.extend(stderr_lines[:5])
        return errors

    def _extract_warnings_from_output(
        self, data: dict | list | None, stderr: str
    ) -> list[str]:
        warnings: list[str] = []
        if isinstance(data, dict):
            for key in ("warnings", "warning"):
                if key in data:
                    val = data[key]
                    if isinstance(val, list):
                        warnings.extend(str(w) for w in val)
                    elif val:
                        warnings.append(str(val))
        if stderr.strip():
            warnings.extend(
                line.strip()
                for line in stderr.splitlines()
                if line.strip().lower().startswith("warning")
            )
        return warnings


__all__ = ["AgentInvoker", "BaseInvoker", "InvocationResult"]
