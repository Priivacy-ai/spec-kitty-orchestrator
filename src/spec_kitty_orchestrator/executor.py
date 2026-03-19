"""Subprocess spawning and log capture for agent executions.

Spawns agent processes asynchronously, captures stdout/stderr to log files,
and enforces timeouts. Uses workspace_path returned by the host API —
does NOT create worktrees or run git directly.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import TextIO

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
    cmd = invoker.build_command(prompt, working_dir, role)
    logger.info("Spawning %s: %s ...", invoker.agent_id, " ".join(cmd[:3]))

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=working_dir,
        )
        logger.debug("Process %s spawned for %s", process.pid, invoker.agent_id)
        return process, cmd
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
    async def _pump_stream(
        stream: asyncio.StreamReader | None,
        label: str,
        sink: bytearray,
        fh: TextIO | None,
        write_lock: asyncio.Lock,
    ) -> None:
        if stream is None:
            return

        first_chunk = True
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            sink.extend(chunk)
            if fh is None:
                continue
            text = chunk.decode("utf-8", errors="replace")
            async with write_lock:
                if first_chunk:
                    fh.write(f"=== {label} ===\n")
                    first_chunk = False
                fh.write(text)
                fh.flush()

    start = time.monotonic()
    stdin_data = prompt.encode("utf-8") if invoker.uses_stdin else None

    process, cmd = await spawn_agent(invoker, prompt, working_dir, role)

    log_handle: TextIO | None = None
    write_lock = asyncio.Lock()
    if log_file:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_handle = open(log_file, "w", encoding="utf-8")
            log_handle.write(f"=== command: {' '.join(cmd)} ===\n")
            log_handle.write(f"=== pid: {process.pid} ===\n")
            log_handle.write("=== status: running ===\n")
            log_handle.flush()
        except OSError as exc:
            logger.warning("Failed to open log file %s: %s", log_file, exc)
            log_handle = None

    stdout_bytes = bytearray()
    stderr_bytes = bytearray()
    try:
        if stdin_data is not None and process.stdin is not None:
            process.stdin.write(stdin_data)
            await process.stdin.drain()
            process.stdin.close()
            wait_closed = getattr(process.stdin, "wait_closed", None)
            if callable(wait_closed):
                await wait_closed()

        stdout_task = asyncio.create_task(
            _pump_stream(process.stdout, "stdout", stdout_bytes, log_handle, write_lock)
        )
        stderr_task = asyncio.create_task(
            _pump_stream(process.stderr, "stderr", stderr_bytes, log_handle, write_lock)
        )

        try:
            await asyncio.wait_for(process.wait(), timeout=float(timeout_seconds))
            exit_code = process.returncode or 0
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
                await process.wait()
            exit_code = TIMEOUT_EXIT_CODE

        await asyncio.gather(stdout_task, stderr_task)
    finally:
        if log_handle is not None:
            try:
                async with write_lock:
                    log_handle.write(f"\n=== exit_code: {exit_code} ===\n")
                    log_handle.flush()
            except OSError:
                pass
            log_handle.close()

    duration = time.monotonic() - start
    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")

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
