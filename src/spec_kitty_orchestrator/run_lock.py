"""Crash-safe single-process lock for orchestration runs."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import uuid
from typing import Iterator


class OrchestrationAlreadyRunningError(RuntimeError):
    """Raised when a live process owns the repository orchestration lock."""


def _process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_owner(lock_file: Path) -> dict[str, object] | None:
    try:
        owner = json.loads(lock_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return owner if isinstance(owner, dict) else None


@contextmanager
def orchestration_lock(lock_file: Path, mission: str) -> Iterator[None]:
    """Exclusively own orchestration in a repository, recovering stale locks."""
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    payload = json.dumps({"pid": os.getpid(), "mission": mission, "token": token})

    while True:
        try:
            fd = os.open(lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            owner = _read_owner(lock_file)
            owner_pid = owner.get("pid") if owner else None
            if isinstance(owner_pid, int) and _process_is_alive(owner_pid):
                owner_mission = (owner or {}).get("mission", "unknown")
                raise OrchestrationAlreadyRunningError(
                    f"orchestration already running for mission '{owner_mission}' "
                    f"(pid {owner_pid})"
                )
            try:
                lock_file.unlink()
            except FileNotFoundError:
                pass
            continue
        else:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            break

    try:
        yield
    finally:
        owner = _read_owner(lock_file)
        if owner and owner.get("token") == token:
            lock_file.unlink(missing_ok=True)


__all__ = ["OrchestrationAlreadyRunningError", "orchestration_lock"]
