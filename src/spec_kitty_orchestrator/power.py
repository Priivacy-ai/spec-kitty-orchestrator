"""Sleep-assertion management for long-lived orchestration runs.

``orchestrate`` runs commonly last 1-3+ hours (implementer + reviewer agents
across many lanes). On macOS, system idle-sleep kills the orchestrator and its
in-flight agent subprocesses mid-run (spec-kitty#2500). Rather than requiring
the operator to remember ``caffeinate -i spec-kitty-orchestrator ...``, the
loop holds an idle-sleep assertion for its own lifetime.

Implementation: spawn ``caffeinate -i -w <own-pid>`` as a child at loop start.
``-w`` ties the assertion to the orchestrator's lifetime with zero polling —
caffeinate exits on its own when this process dies, including on crashes and
SIGKILL, so the assertion can never leak. On clean exit the child is also
terminated eagerly for immediate release.

Scope: idle sleep only (``-i``). Lid-close sleep is an OS policy decision the
tool does not fight. Non-macOS platforms are a no-op (Linux servers don't
idle-sleep under load; ``systemd-inhibit`` support would be a follow-up).
"""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import sys
from collections.abc import Iterator

logger = logging.getLogger(__name__)

_CAFFEINATE_PATH = "/usr/bin/caffeinate"
_CLEANUP_TIMEOUT_SECONDS = 1.0


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


@contextlib.contextmanager
def prevent_idle_sleep(enabled: bool = True) -> Iterator[None]:
    """Hold a macOS idle-sleep assertion for the duration of the block.

    Args:
        enabled: When False (``--no-caffeinate``), do nothing — the operator
            intentionally wants sleep to be able to interrupt the run.

    Never raises: a missing ``caffeinate`` binary or spawn failure logs a
    warning and the orchestration proceeds without the assertion.
    """
    proc: subprocess.Popen[bytes] | None = None
    if enabled and sys.platform == "darwin":
        try:
            proc = subprocess.Popen(
                [_CAFFEINATE_PATH, "-i", "-w", str(os.getpid())],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            logger.info(
                "Holding idle-sleep assertion for the orchestration run "
                "(caffeinate pid %d; disable with --no-caffeinate)",
                proc.pid,
            )
        except OSError as exc:
            logger.warning(
                "Could not hold idle-sleep assertion (%s); the system may "
                "idle-sleep during long runs. Consider running under "
                "`caffeinate -i` manually.",
                exc,
            )
            proc = None
    try:
        yield
    finally:
        if proc is not None:
            # -w releases on our exit anyway; terminate eagerly so the
            # assertion drops the moment the loop finishes, not at
            # process teardown.
            _release_assertion(proc)
