"""Tests for the idle-sleep assertion held during orchestration runs (#2500)."""

from __future__ import annotations

import os
import subprocess
from unittest.mock import MagicMock

from spec_kitty_orchestrator import power
from spec_kitty_orchestrator.power import prevent_idle_sleep


def _spawn_spy(monkeypatch, *, raise_exc: OSError | None = None):
    """Patch subprocess.Popen in the power module; return (calls, proc mock)."""
    calls: list[list[str]] = []
    proc = MagicMock()
    proc.pid = 4242
    proc.wait.return_value = 0

    def _fake_popen(args, **_kwargs):
        if raise_exc is not None:
            raise raise_exc
        calls.append(list(args))
        return proc

    monkeypatch.setattr(power.subprocess, "Popen", _fake_popen)
    return calls, proc


def test_holds_assertion_on_darwin(monkeypatch) -> None:
    monkeypatch.setattr(power.sys, "platform", "darwin")
    calls, proc = _spawn_spy(monkeypatch)

    with prevent_idle_sleep():
        assert calls == [["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())]]
        proc.terminate.assert_not_called()

    # Released eagerly on clean exit.
    proc.terminate.assert_called_once()
    proc.wait.assert_called_once_with(timeout=1.0)


def test_releases_assertion_on_exception(monkeypatch) -> None:
    monkeypatch.setattr(power.sys, "platform", "darwin")
    _calls, proc = _spawn_spy(monkeypatch)

    try:
        with prevent_idle_sleep():
            raise RuntimeError("loop blew up")
    except RuntimeError:
        pass

    proc.terminate.assert_called_once()
    proc.wait.assert_called_once_with(timeout=1.0)


def test_disabled_is_a_noop(monkeypatch) -> None:
    monkeypatch.setattr(power.sys, "platform", "darwin")
    calls, proc = _spawn_spy(monkeypatch)

    with prevent_idle_sleep(enabled=False):
        pass

    assert calls == []
    proc.terminate.assert_not_called()


def test_non_darwin_is_a_noop(monkeypatch) -> None:
    monkeypatch.setattr(power.sys, "platform", "linux")
    calls, proc = _spawn_spy(monkeypatch)

    with prevent_idle_sleep():
        pass

    assert calls == []
    proc.terminate.assert_not_called()


def test_missing_caffeinate_degrades_gracefully(monkeypatch, caplog) -> None:
    """A missing binary must warn and proceed, never break the run."""
    monkeypatch.setattr(power.sys, "platform", "darwin")
    _spawn_spy(monkeypatch, raise_exc=FileNotFoundError("caffeinate not found"))

    ran = False
    with caplog.at_level("WARNING"):
        with prevent_idle_sleep():
            ran = True

    assert ran
    assert "Could not hold idle-sleep assertion" in caplog.text


def test_terminate_failure_is_swallowed(monkeypatch) -> None:
    """A dead caffeinate child at exit must not raise out of the finally."""
    monkeypatch.setattr(power.sys, "platform", "darwin")
    _calls, proc = _spawn_spy(monkeypatch)
    proc.terminate.side_effect = OSError("already gone")

    with prevent_idle_sleep():
        pass  # exiting must not raise

    proc.wait.assert_called_once_with(timeout=1.0)


def test_cleanup_kills_and_reaps_helper_that_ignores_terminate(monkeypatch) -> None:
    """Cleanup remains bounded even if the helper ignores SIGTERM."""
    monkeypatch.setattr(power.sys, "platform", "darwin")
    _calls, proc = _spawn_spy(monkeypatch)
    proc.wait.side_effect = [
        subprocess.TimeoutExpired(cmd="/usr/bin/caffeinate", timeout=1.0),
        0,
    ]

    with prevent_idle_sleep():
        pass

    proc.terminate.assert_called_once()
    proc.kill.assert_called_once()
    assert proc.wait.call_count == 2
