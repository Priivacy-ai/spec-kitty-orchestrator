from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from spec_kitty_orchestrator.agents.base import InvocationResult
from spec_kitty_orchestrator.agents.gemini import GeminiInvoker, GEMINI_EXIT_RATE_LIMIT
from spec_kitty_orchestrator.executor import execute_agent


@pytest.mark.asyncio
async def test_execute_agent_creates_live_log_file_before_process_exit(tmp_path: Path) -> None:
    class _FakeStream:
        def __init__(self, chunks: list[bytes]) -> None:
            self._chunks = list(chunks)

        async def read(self, n: int = -1) -> bytes:
            if self._chunks:
                await asyncio.sleep(0)
                return self._chunks.pop(0)
            return b""

    class _FakeStdin:
        def __init__(self) -> None:
            self.writes: list[bytes] = []
            self.closed = False

        def write(self, data: bytes) -> None:
            self.writes.append(data)

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            self.closed = True

        async def wait_closed(self) -> None:
            return None

    class _FakeProcess:
        def __init__(self) -> None:
            self.pid = 4242
            self.stdin = _FakeStdin()
            self.stdout = _FakeStream([b'{"result":"ok"}\n'])
            self.stderr = _FakeStream([b"warning: test\n"])
            self.returncode = 0

        async def wait(self) -> int:
            await asyncio.sleep(0)
            return 0

    invoker = MagicMock()
    invoker.uses_stdin = True
    invoker.agent_id = "gemini"
    invoker.detect_runtime_termination.return_value = None
    invoker.parse_output.return_value = InvocationResult(
        success=True,
        exit_code=0,
        stdout='{"result":"ok"}\n',
        stderr="warning: test\n",
        duration_seconds=0.01,
    )

    process = _FakeProcess()
    log_file = tmp_path / "impl.log"

    with patch(
        "spec_kitty_orchestrator.executor.spawn_agent",
        new=AsyncMock(return_value=(process, ["gemini", "--json"])),
    ):
        task = asyncio.create_task(
            execute_agent(
                invoker,
                "prompt text",
                tmp_path,
                "implementation",
                timeout_seconds=5,
                log_file=log_file,
            )
        )
        await asyncio.sleep(0)
        assert log_file.exists()
        log_text = log_file.read_text(encoding="utf-8")
        assert "=== status: running ===" in log_text
        assert "=== pid: 4242 ===" in log_text

        result = await task

    assert result is invoker.parse_output.return_value
    final_log = log_file.read_text(encoding="utf-8")
    assert "=== stdout ===" in final_log
    assert '{"result":"ok"}' in final_log
    assert "=== stderr ===" in final_log
    assert "warning: test" in final_log
    assert "=== exit_code: 0 ===" in final_log


@pytest.mark.asyncio
async def test_execute_agent_terminates_early_on_gemini_capacity_error(tmp_path: Path) -> None:
    class _FakeStream:
        def __init__(self, chunks: list[bytes]) -> None:
            self._chunks = list(chunks)

        async def read(self, n: int = -1) -> bytes:
            if self._chunks:
                await asyncio.sleep(0)
                return self._chunks.pop(0)
            await asyncio.sleep(0)
            return b""

    class _FakeStdin:
        def write(self, data: bytes) -> None:
            return None

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

    class _BlockingProcess:
        def __init__(self) -> None:
            self.pid = 777
            self.stdin = _FakeStdin()
            self.stdout = _FakeStream([])
            self.stderr = _FakeStream([
                b'Attempt 1 failed with status 429.\n',
                b'No capacity available for model gemini-2.5-flash on the server\n',
            ])
            self.returncode = None
            self._done = asyncio.Event()

        async def wait(self) -> int:
            await self._done.wait()
            return self.returncode or 0

        def terminate(self) -> None:
            self.returncode = -15
            self._done.set()

        def kill(self) -> None:
            self.returncode = -9
            self._done.set()

    invoker = GeminiInvoker()
    log_file = tmp_path / "review.log"
    process = _BlockingProcess()

    with patch(
        "spec_kitty_orchestrator.executor.spawn_agent",
        new=AsyncMock(return_value=(process, ["gemini", "--json"])),
    ):
        result = await execute_agent(
            invoker,
            "prompt text",
            tmp_path,
            "review",
            timeout_seconds=30,
            log_file=log_file,
        )

    assert result.exit_code == GEMINI_EXIT_RATE_LIMIT
    assert result.success is False
    assert any("rate limit" in err.lower() for err in result.errors)
    log_text = log_file.read_text(encoding="utf-8")
    assert "early_termination" in log_text


@pytest.mark.asyncio
async def test_execute_agent_handles_stdin_connection_reset(tmp_path: Path) -> None:
    class _FakeStream:
        def __init__(self, chunks: list[bytes]) -> None:
            self._chunks = list(chunks)

        async def read(self, n: int = -1) -> bytes:
            if self._chunks:
                await asyncio.sleep(0)
                return self._chunks.pop(0)
            return b""

    class _ResettingStdin:
        def write(self, data: bytes) -> None:
            return None

        async def drain(self) -> None:
            raise ConnectionResetError("Connection lost")

        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            raise ConnectionResetError("Connection lost")

    class _FakeProcess:
        def __init__(self) -> None:
            self.pid = 5252
            self.stdin = _ResettingStdin()
            self.stdout = _FakeStream([])
            self.stderr = _FakeStream([b"terminated before prompt\n"])
            self.returncode = 124

        async def wait(self) -> int:
            await asyncio.sleep(0)
            return self.returncode

    invoker = MagicMock()
    invoker.uses_stdin = True
    invoker.agent_id = "gemini"
    invoker.detect_runtime_termination.return_value = None
    invoker.parse_output.return_value = InvocationResult(
        success=False,
        exit_code=124,
        stdout="",
        stderr="terminated before prompt\n",
        duration_seconds=0.01,
        errors=["terminated before prompt"],
    )

    process = _FakeProcess()
    log_file = tmp_path / "reset.log"

    with patch(
        "spec_kitty_orchestrator.executor.spawn_agent",
        new=AsyncMock(return_value=(process, ["gemini", "--json"])),
    ):
        result = await execute_agent(
            invoker,
            "prompt text",
            tmp_path,
            "review",
            timeout_seconds=5,
            log_file=log_file,
        )

    assert result is invoker.parse_output.return_value
    assert log_file.exists()
    assert "exit_code: 124" in log_file.read_text(encoding="utf-8")
