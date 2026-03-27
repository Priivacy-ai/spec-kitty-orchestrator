"""Subprocess spawning and log capture for agent executions.

Spawns agent processes asynchronously, captures stdout/stderr to log files,
and enforces timeouts. Uses workspace_path returned by the host API —
does NOT create worktrees or run git directly.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

from .agents.base import AgentInvoker, BaseInvoker, InvocationResult

logger = logging.getLogger(__name__)

TIMEOUT_EXIT_CODE = 124
TERMINATION_GRACE_SECONDS = 5.0


class ExecutorError(Exception):
    """Base exception for executor errors."""


class ProcessSpawnError(ExecutorError):
    """Raised when process spawning fails."""


class ExecutionTimeoutError(ExecutorError):
    """Raised when an agent execution exceeds the timeout."""


def get_log_path(log_dir: Path, feature: str, wp_id: str, role: str) -> Path:
    """Return the log file path for a given WP execution.

    Args:
        log_dir: Base log directory (provider-owned).
        feature: Feature slug.
        wp_id: Work package ID.
        role: "implementation" or "review".

    Returns:
        Path to the log file (not yet created).
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / f"{feature}_{wp_id}_{role}.log"


async def spawn_agent(
    invoker: BaseInvoker,
    prompt: str,
    working_dir: Path,
    role: str,
) -> tuple[asyncio.subprocess.Process, list[str]]:
    """Spawn an agent subprocess.

    Args:
        invoker: Agent invoker.
        prompt: Task prompt (sent via stdin if invoker.uses_stdin).
        working_dir: Directory where agent should run.
        role: "implementation" or "review".

    Returns:
        (process, cmd) tuple.

    Raises:
        ProcessSpawnError: If the process cannot be spawned.
    """
    if not working_dir.exists():
        raise ProcessSpawnError(f"Working directory does not exist: {working_dir}")

    cmd = invoker.build_command(prompt, working_dir, role)
    executable = cmd[0]
    args = cmd[1:]
    resolved = shutil.which(executable) or executable

    use_shell = False
    spawn_cmd = [resolved] + list(args)

    if os.name == "nt" and resolved.lower().endswith((".cmd", ".bat")):
        use_shell = True
        shell_cmd = f'"{resolved}" {subprocess.list2cmdline(args)}'
        logger.info("Spawning %s via shell: %s", invoker.agent_id, shell_cmd)
    else:
        logger.info("Spawning %s: %s ...", invoker.agent_id, " ".join(spawn_cmd[:3]))

    try:
        if use_shell:
            process = await asyncio.create_subprocess_shell(
                shell_cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=working_dir,
            )
        else:
            process = await asyncio.create_subprocess_exec(
                *spawn_cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=working_dir,
            )
        logger.debug("Process %s spawned for %s", process.pid, invoker.agent_id)
        return process, spawn_cmd
    except OSError as exc:
        raise ProcessSpawnError(
            f"Failed to spawn {invoker.agent_id}: {exc}"
        ) from exc


async def execute_with_timeout(
    process: asyncio.subprocess.Process,
    stdin_data: bytes | None,
    timeout_seconds: int,
) -> tuple[bytes, bytes, int]:
    """Wait for process with timeout; kill gracefully if exceeded.

    Args:
        process: The spawned asyncio subprocess.
        stdin_data: Bytes to send to stdin (None if not uses_stdin).
        timeout_seconds: Maximum allowed execution time.

    Returns:
        (stdout_bytes, stderr_bytes, exit_code) — exit_code is TIMEOUT_EXIT_CODE
        if the process was killed due to timeout.
    """
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(input=stdin_data),
            timeout=float(timeout_seconds),
        )
        return stdout_bytes, stderr_bytes, process.returncode or 0
    except asyncio.TimeoutError:
        logger.warning("Process %s timed out after %ss", process.pid, timeout_seconds)
        try:
            process.terminate()
            await asyncio.wait_for(process.wait(), timeout=TERMINATION_GRACE_SECONDS)
        except (asyncio.TimeoutError, ProcessLookupError):
            try:
                process.kill()
            except ProcessLookupError:
                pass
        return b"", b"", TIMEOUT_EXIT_CODE


async def execute_agent(
    invoker: BaseInvoker,
    prompt: str,
    working_dir: Path,
    role: str,
    timeout_seconds: int,
    log_file: Path | None = None,
) -> InvocationResult:
    """Execute an agent and return a structured InvocationResult.

    Handles stdin piping, timeout, log capture.

    Args:
        invoker: Agent invoker instance.
        prompt: Task prompt text.
        working_dir: Directory for agent execution.
        role: "implementation" or "review".
        timeout_seconds: Maximum execution time.
        log_file: Optional path to write combined stdout+stderr.

    Returns:
        InvocationResult with all captured output.
    """
    start = time.monotonic()
    stdin_data = prompt.encode("utf-8") if invoker.uses_stdin else None

    process, cmd = await spawn_agent(invoker, prompt, working_dir, role)
    stdout_bytes, stderr_bytes, exit_code = await execute_with_timeout(
        process, stdin_data, timeout_seconds
    )

    duration = time.monotonic() - start
    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")

    if log_file:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            with open(log_file, "w", encoding="utf-8") as fh:
                fh.write(f"=== command: {' '.join(cmd)} ===\n")
                fh.write(f"=== exit_code: {exit_code} ===\n")
                fh.write(f"=== stdout ===\n{stdout}\n")
                fh.write(f"=== stderr ===\n{stderr}\n")
        except OSError as exc:
            logger.warning("Failed to write log file %s: %s", log_file, exc)

    logger.info(
        "%s %s/%s finished: exit=%d, duration=%.1fs",
        invoker.agent_id, role, working_dir.name, exit_code, duration,
    )
    return invoker.parse_output(stdout, stderr, exit_code, duration)


__all__ = [
    "ExecutorError",
    "ProcessSpawnError",
    "ExecutionTimeoutError",
    "get_log_path",
    "execute_agent",
    "TIMEOUT_EXIT_CODE",
]
