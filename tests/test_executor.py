from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from spec_kitty_orchestrator.agents.base import InvocationResult
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
