from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from spec_kitty_orchestrator.executor import ProcessSpawnError, spawn_agent


@pytest.mark.asyncio
async def test_spawn_agent_uses_shell_for_windows_batch_shim() -> None:
    invoker = MagicMock()
    invoker.agent_id = "test-agent"
    invoker.build_command.return_value = ["mycmd", "--flag"]
    working_dir = Path(__file__).parent

    with (
        patch("os.name", "nt"),
        patch("shutil.which", return_value=r"C:\npm\mycmd.cmd"),
        patch("asyncio.create_subprocess_shell", new_callable=AsyncMock) as mock_shell,
    ):
        mock_shell.return_value.pid = 123

        process, cmd = await spawn_agent(invoker, "prompt", working_dir, "implementation")

    assert process.pid == 123
    assert cmd == [r"C:\npm\mycmd.cmd", "--flag"]
    mock_shell.assert_called_once()
    shell_cmd = mock_shell.call_args.args[0]
    assert '"C:\\npm\\mycmd.cmd"' in shell_cmd
    assert "--flag" in shell_cmd


@pytest.mark.asyncio
async def test_spawn_agent_uses_exec_for_normal_executable() -> None:
    invoker = MagicMock()
    invoker.agent_id = "test-agent"
    invoker.build_command.return_value = ["myexe", "--flag"]
    working_dir = Path(__file__).parent

    with (
        patch("os.name", "posix"),
        patch("shutil.which", return_value="/usr/bin/myexe"),
        patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec,
    ):
        mock_exec.return_value.pid = 456

        process, cmd = await spawn_agent(invoker, "prompt", working_dir, "implementation")

    assert process.pid == 456
    assert cmd == ["/usr/bin/myexe", "--flag"]
    mock_exec.assert_called_once_with(
        "/usr/bin/myexe",
        "--flag",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=working_dir,
    )


@pytest.mark.asyncio
async def test_spawn_agent_validates_working_dir() -> None:
    invoker = MagicMock()
    working_dir = Path("/non/existent/path/99999")

    with pytest.raises(ProcessSpawnError, match="Working directory does not exist"):
        await spawn_agent(invoker, "prompt", working_dir, "implementation")
