"""Sleep-assertion management for long-lived orchestration runs.

``orchestrate`` runs commonly last 1-3+ hours (implementer + reviewer agents
across many lanes). System idle-sleep kills the orchestrator and its in-flight
agent subprocesses mid-run (spec-kitty#2500). Rather than requiring the
operator to remember a platform-specific incantation, the loop holds an
idle-sleep assertion for its own lifetime, on whichever OS it's running on.

Implementation is platform-specific but follows the same shape everywhere:
hold the assertion for exactly the lifetime of this process, release it
eagerly on clean exit, and never let a missing tool or failed call break the
run — degrade to a no-op with a warning instead.

- macOS: spawn ``caffeinate -i -w <own-pid>`` as a child at loop start. ``-w``
  ties the assertion to the orchestrator's lifetime with zero polling —
  caffeinate exits on its own when this process dies, including on crashes
  and SIGKILL, so the assertion can never leak.
- Linux: spawn ``systemd-inhibit --what=idle:sleep -- tail --pid <own-pid> -f
  /dev/null`` as a child. ``tail --pid`` mirrors caffeinate's ``-w``: it exits
  once the watched pid is gone, so the inhibitor lock releases itself even on
  a crash. Requires systemd (present on most modern distros); a no-op with a
  warning on non-systemd Linux (Alpine, minimal containers, etc.).
- Windows: call the Win32 ``SetThreadExecutionState`` API directly via
  ``ctypes`` (stdlib only, no subprocess) with ``ES_SYSTEM_REQUIRED``; reset
  to ``ES_CONTINUOUS`` alone on exit. Sticky — no polling needed while held.

Scope: idle sleep only. Lid-close sleep is an OS policy decision the tool
does not fight.
"""

from __future__ import annotations

import contextlib
import ctypes
import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator

logger = logging.getLogger(__name__)

_CAFFEINATE_PATH = "/usr/bin/caffeinate"
_CLEANUP_TIMEOUT_SECONDS = 1.0

# Win32 SetThreadExecutionState flags (winbase.h). ES_CONTINUOUS alone clears
# any previously-set state; combined with ES_SYSTEM_REQUIRED it holds the
# system-sleep assertion until changed back or the process exits.
_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001


def _release_assertion(proc: subprocess.Popen[bytes]) -> None:
    """Stop and reap the assertion helper without disrupting orchestration."""
    with contextlib.suppress(OSError):
        proc.terminate()

    try:
        proc.wait(timeout=_CLEANUP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            proc.kill()
        try:
            proc.wait(timeout=_CLEANUP_TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("Could not reap idle-sleep assertion helper: %s", exc)
    except OSError as exc:
        logger.debug("Idle-sleep assertion helper was already reaped: %s", exc)


def _spawn_caffeinate() -> subprocess.Popen[bytes] | None:
    """Spawn the macOS idle-sleep assertion helper. Never raises."""
    try:
        proc = subprocess.Popen(
            [_CAFFEINATE_PATH, "-i", "-w", str(os.getpid())],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        logger.warning(
            "Could not hold idle-sleep assertion (%s); the system may "
            "idle-sleep during long runs. Consider running under "
            "`caffeinate -i` manually.",
            exc,
        )
        return None
    logger.info(
        "Holding idle-sleep assertion for the orchestration run "
        "(caffeinate pid %d; disable with --no-caffeinate)",
        proc.pid,
    )
    return proc


def _spawn_systemd_inhibit() -> subprocess.Popen[bytes] | None:
    """Spawn the Linux idle-sleep assertion helper. Never raises."""
    inhibit_bin = shutil.which("systemd-inhibit")
    if inhibit_bin is None:
        logger.warning(
            "systemd-inhibit not found; the system may idle-sleep during "
            "long runs. Install systemd, or hold an inhibitor manually "
            "(disable this check with --no-caffeinate)."
        )
        return None
    try:
        proc = subprocess.Popen(
            [
                inhibit_bin,
                "--what=idle:sleep",
                "--who=spec-kitty-orchestrator",
                "--why=long-running orchestration run",
                "tail",
                "--pid",
                str(os.getpid()),
                "-f",
                "/dev/null",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        logger.warning(
            "Could not hold idle-sleep assertion (%s); the system may "
            "idle-sleep during long runs. Consider running under "
            "`systemd-inhibit` manually.",
            exc,
        )
        return None
    logger.info(
        "Holding idle-sleep assertion for the orchestration run "
        "(systemd-inhibit pid %d; disable with --no-caffeinate)",
        proc.pid,
    )
    return proc


def _set_thread_execution_state(flags: int) -> bool:
    """Call Win32 ``SetThreadExecutionState``. Isolated for non-Windows testability."""
    return bool(ctypes.windll.kernel32.SetThreadExecutionState(flags))  # type: ignore[attr-defined]


def _hold_windows_execution_state() -> bool:
    """Hold the Windows idle-sleep assertion for this process. Never raises."""
    try:
        held = _set_thread_execution_state(_ES_CONTINUOUS | _ES_SYSTEM_REQUIRED)
    except OSError as exc:
        logger.warning(
            "Could not hold idle-sleep assertion (%s); the system may "
            "idle-sleep during long runs.",
            exc,
        )
        return False
    if not held:
        logger.warning(
            "SetThreadExecutionState reported failure; the system may "
            "idle-sleep during long runs."
        )
        return False
    logger.info(
        "Holding idle-sleep assertion for the orchestration run "
        "(SetThreadExecutionState; disable with --no-caffeinate)"
    )
    return True


@contextlib.contextmanager
def prevent_idle_sleep(enabled: bool = True) -> Iterator[None]:
    """Hold an idle-sleep assertion for the duration of the block.

    Args:
        enabled: When False (``--no-caffeinate``), do nothing — the operator
            intentionally wants sleep to be able to interrupt the run.

    Never raises: a missing platform tool, a failed spawn, or a failed API
    call logs a warning and the orchestration proceeds without the
    assertion. Platforms with no known mechanism are a no-op.
    """
    proc: subprocess.Popen[bytes] | None = None
    windows_state_held = False
    if enabled:
        if sys.platform == "darwin":
            proc = _spawn_caffeinate()
        elif sys.platform.startswith("linux"):
            proc = _spawn_systemd_inhibit()
        elif sys.platform == "win32":
            windows_state_held = _hold_windows_execution_state()
    try:
        yield
    finally:
        if proc is not None:
            # The helper releases on our exit anyway (caffeinate -w /
            # tail --pid); terminate eagerly so the assertion drops the
            # moment the loop finishes, not at process teardown.
            _release_assertion(proc)
        if windows_state_held:
            with contextlib.suppress(OSError):
                _set_thread_execution_state(_ES_CONTINUOUS)
