import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import openharness.tools.bash_tool as bash_tool_module
from openharness.tools.base import ToolExecutionContext
from openharness.tools.bash_tool import BashTool, BashToolInput


class _FakeStdout:
    def __init__(self, chunks: list[bytes], *, sleep_forever: bool = False):
        self._chunks = list(chunks)
        self._sleep_forever = sleep_forever
        self._process = None

    def attach(self, process) -> None:
        self._process = process

    async def read(self, _size: int = -1):
        if self._chunks:
            if _size == -1:
                chunks = self._chunks[:]
                self._chunks.clear()
                return b"".join(chunks)
            total = bytearray()
            while self._chunks and (len(total) < _size):
                next_chunk = self._chunks[0]
                remaining = _size - len(total)
                if len(next_chunk) <= remaining:
                    total.extend(self._chunks.pop(0))
                    continue
                total.extend(next_chunk[:remaining])
                self._chunks[0] = next_chunk[remaining:]
                break
            return bytes(total)
        if self._process is not None and self._process.returncode is not None:
            return b""
        if self._sleep_forever:
            await asyncio.sleep(0.05)
            if self._process is not None and self._process.returncode is not None:
                return b""
        return b""


class _FakeProcess:
    def __init__(self, *, stdout=None, returncode=None):
        self.stdout = stdout
        self.returncode = returncode
        self.terminated = False
        self.killed = False
        if hasattr(self.stdout, "attach"):
            self.stdout.attach(self)

    async def wait(self):
        if self.returncode is None:
            await asyncio.sleep(60)
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9


class _NeverClosingStdout:
    async def read(self, _size: int = -1):
        await asyncio.sleep(60)
        return b""


@pytest.mark.asyncio
async def test_terminate_process_uses_taskkill_tree_on_windows(monkeypatch):
    process = _FakeProcess(stdout=None)
    process.pid = 4321
    calls: list[tuple[str, ...]] = []

    class _Killer:
        async def wait(self) -> int:
            return 0

    async def fake_exec(*args, **kwargs):
        calls.append(args)
        return _Killer()

    monkeypatch.setattr("openharness.tools.bash_tool.sys.platform", "win32")
    monkeypatch.setattr("openharness.tools.bash_tool.asyncio.create_subprocess_exec", fake_exec)

    await bash_tool_module._terminate_process(process, force=True)

    assert calls == [("taskkill", "/F", "/T", "/PID", "4321")]
    assert process.returncode is not None


@pytest.mark.asyncio
async def test_bash_tool_preflight_short_circuits_interactive_scaffold_even_with_timeout_fixture(monkeypatch, tmp_path: Path):
    process = _FakeProcess(
        stdout=_FakeStdout(
            [
                b"Creating a new Next.js app in /tmp/coolblog.\n",
                b"Would you like to use Turbopack? \n",
            ],
            sleep_forever=True,
        )
    )

    async def fake_create_shell_subprocess(*args, **kwargs):
        return process

    monkeypatch.setitem(BashTool.execute.__globals__, "create_shell_subprocess", fake_create_shell_subprocess)

    result = await BashTool().execute(
        BashToolInput(
            command='npx create-next-app@latest coolblog --typescript --tailwind --eslint --app --src-dir --import-alias "@/*"',
            timeout_seconds=1,
        ),
        ToolExecutionContext(cwd=tmp_path),
    )

    assert result.is_error is True
    assert "This command appears to require interactive input before it can continue." in result.output
    assert result.metadata["interactive_required"] is True


@pytest.mark.asyncio
async def test_bash_tool_preflights_interactive_scaffold_commands(tmp_path: Path):
    result = await BashTool().execute(
        BashToolInput(
            command='npx create-next-app@latest coolblog --typescript --tailwind --eslint --app --src-dir --import-alias "@/*"',
            timeout_seconds=1,
        ),
        ToolExecutionContext(cwd=tmp_path),
    )

    assert result.is_error is True
    assert result.metadata["interactive_required"] is True
    assert "cannot answer installer/scaffold prompts live" in result.output
    assert "non-interactive flags" in result.output


@pytest.mark.asyncio
async def test_bash_tool_timeout_returns_partial_output_for_real_command(tmp_path: Path):
    result = await BashTool().execute(
        BashToolInput(
            command=(
                f'"{sys.executable}" -u -c "print(\'Creating a new Next.js app in /tmp/coolblog.\'); '
                "print('Would you like to use Turbopack?'); "
                "import time; time.sleep(5)\""
            ),
            timeout_seconds=1,
        ),
        ToolExecutionContext(cwd=tmp_path),
    )

    assert result.is_error is True
    assert "Command timed out after 1 seconds." in result.output
    assert "Partial output:" in result.output
    assert "Creating a new Next.js app in /tmp/coolblog." in result.output
    assert "Would you like to use Turbopack?" in result.output
    assert "This command appears to require interactive input." in result.output
    assert result.metadata["timed_out"] is True


@pytest.mark.asyncio
async def test_bash_tool_collects_combined_output(monkeypatch, tmp_path: Path):
    process = _FakeProcess(
        stdout=_FakeStdout([b"line one\n", b"line two\n", b""]),
        returncode=0,
    )

    async def fake_create_shell_subprocess(*args, **kwargs):
        return process

    monkeypatch.setitem(BashTool.execute.__globals__, "create_shell_subprocess", fake_create_shell_subprocess)

    result = await BashTool().execute(
        BashToolInput(command="printf 'line one\\nline two\\n'"),
        ToolExecutionContext(cwd=tmp_path),
    )

    assert result.is_error is False
    assert result.output == "line one\nline two"
    assert result.metadata["returncode"] == 0


@pytest.mark.asyncio
async def test_bash_tool_uses_devnull_stdin_for_non_interactive_shell(monkeypatch, tmp_path: Path):
    process = _FakeProcess(
        stdout=_FakeStdout([b"ok\n", b""]),
        returncode=0,
    )
    seen_kwargs: dict[str, object] = {}

    async def fake_create_shell_subprocess(*args, **kwargs):
        del args
        seen_kwargs.update(kwargs)
        return process

    monkeypatch.setitem(BashTool.execute.__globals__, "create_shell_subprocess", fake_create_shell_subprocess)

    result = await BashTool().execute(
        BashToolInput(command="echo ok"),
        ToolExecutionContext(cwd=tmp_path),
    )

    assert result.is_error is False
    assert seen_kwargs["stdin"] == asyncio.subprocess.DEVNULL
    assert seen_kwargs["prefer_pty"] is True


@pytest.mark.asyncio
async def test_bash_tool_drains_output_larger_than_the_pipe_buffer(tmp_path: Path):
    # Waiting for exit before reading stdout deadlocks once the child fills
    # the OS pipe buffer; this command writes ~200 KiB and then exits.
    result = await BashTool().execute(
        BashToolInput(
            command=f'"{sys.executable}" -c "print(\'x\' * 200000)"',
            timeout_seconds=10,
        ),
        ToolExecutionContext(cwd=tmp_path),
    )

    assert result.is_error is False
    assert result.output.startswith("xxx")
    assert "[truncated]" in result.output


@pytest.mark.asyncio
async def test_bash_tool_timeout_does_not_hang_when_stdout_stays_open(monkeypatch, tmp_path: Path):
    process = _FakeProcess(stdout=_NeverClosingStdout())

    async def fake_create_shell_subprocess(*args, **kwargs):
        return process

    monkeypatch.setattr("openharness.tools.bash_tool.create_shell_subprocess", fake_create_shell_subprocess)
    monkeypatch.setattr(
        bash_tool_module,
        "_READ_REMAINING_OUTPUT_TIMEOUT_SECONDS",
        0.05,
        raising=False,
    )

    result = await asyncio.wait_for(
        BashTool().execute(
            BashToolInput(command="sleep 10", timeout_seconds=1),
            ToolExecutionContext(cwd=tmp_path),
        ),
        timeout=5.0,
    )

    assert result.is_error is True
    assert result.metadata["timed_out"] is True


def test_bash_timeout_allows_up_to_one_hour_and_clamps_larger_values():
    assert BashToolInput(command="make", timeout_seconds=620).timeout_seconds == 620
    assert BashToolInput(command="make", timeout_seconds=3600).timeout_seconds == 3600
    assert BashToolInput(command="make", timeout_seconds=99_999).timeout_seconds == 3600
    assert BashToolInput(command="make", timeout_seconds=0).timeout_seconds == 1


@pytest.mark.asyncio
async def test_background_event_hands_the_running_command_to_the_task_manager(
    monkeypatch, tmp_path: Path
):
    """Ctrl+B: the still-running foreground command becomes a background task."""
    process = _FakeProcess(stdout=_FakeStdout([b"partial output\n"], sleep_forever=True))

    async def fake_create_shell_subprocess(*args, **kwargs):
        return process

    monkeypatch.setitem(
        BashTool.execute.__globals__, "create_shell_subprocess", fake_create_shell_subprocess
    )

    adopted: dict[str, object] = {}

    class _Manager:
        async def adopt_shell_process(self, **kwargs):
            adopted.update(kwargs)
            return SimpleNamespace(id="task-42")

    monkeypatch.setattr("openharness.tasks.manager.get_task_manager", lambda: _Manager())

    background_event = asyncio.Event()
    background_event.set()

    result = await asyncio.wait_for(
        BashTool().execute(
            BashToolInput(command="npm run build", timeout_seconds=30),
            ToolExecutionContext(cwd=tmp_path, background_event=background_event),
        ),
        timeout=5.0,
    )

    assert result.is_error is False
    assert result.metadata["backgrounded"] is True
    assert result.metadata["task_id"] == "task-42"
    assert "task-42" in result.output
    assert "partial output" in result.output
    assert adopted["process"] is process
    assert adopted["initial_output"] == b"partial output\n"
    assert adopted["command"] == "npm run build"


@pytest.mark.asyncio
async def test_a_command_that_finishes_first_never_touches_the_task_manager(
    monkeypatch, tmp_path: Path
):
    class _ImmediateProcess(_FakeProcess):
        async def wait(self):
            for _ in range(10):
                if self.returncode is not None:
                    return self.returncode
                await asyncio.sleep(0.01)
            return 0

    process = _ImmediateProcess(stdout=_FakeStdout([b"done\n"]), returncode=0)

    async def fake_create_shell_subprocess(*args, **kwargs):
        return process

    monkeypatch.setitem(
        BashTool.execute.__globals__, "create_shell_subprocess", fake_create_shell_subprocess
    )

    def _fail_manager():
        raise AssertionError("the task manager must not be touched")

    monkeypatch.setattr("openharness.tasks.manager.get_task_manager", _fail_manager)

    result = await asyncio.wait_for(
        BashTool().execute(
            BashToolInput(command="echo done", timeout_seconds=5),
            ToolExecutionContext(cwd=tmp_path, background_event=asyncio.Event()),
        ),
        timeout=5.0,
    )

    assert "done" in result.output
    assert "backgrounded" not in result.metadata
