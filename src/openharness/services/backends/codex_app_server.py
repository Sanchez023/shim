"""Codex app-server JSON-RPC backend.

The protocol mirrors ``codex-rs/app-server`` and the reference adapter in
DeepSeek Harness ``packages/subagent/subagent-codex/src/wire.ts``:

``initialize`` -> ``initialized`` -> ``thread/start`` -> ``turn/start`` and
wait for ``turn/completed``.  The first implementation is deliberately
one-shot and unattended; long-lived threads and interactive approvals are left
to a later iteration.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shlex
import shutil
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from openharness.domain.messages import ConversationMessage, TextBlock
from openharness.domain.stream_events import AssistantTurnComplete, StreamEvent
from openharness.domain.usage import UsageSnapshot
from openharness.services.agent_backend import AgentCapabilities
from openharness.services.subagent_provider import (
    SubagentCapabilities,
    SubagentResult,
    SubagentRun,
    SubagentStartRequest,
)


def default_codex_command() -> list[str]:
    """Resolve a runnable Codex CLI command.

    Preference order: explicit ``OPENHARNESS_CODEX_COMMAND``, a ``codex``
    binary on PATH, then ``npx @openai/codex``.
    """
    configured = os.environ.get("OPENHARNESS_CODEX_COMMAND", "").strip()
    if configured:
        return shlex.split(configured, posix=os.name != "nt")
    if shutil.which("codex"):
        return ["codex"]
    npx = shutil.which("npx")
    if npx:
        if os.name == "nt":
            return ["cmd", "/c", npx, "--yes", "@openai/codex"]
        return [npx, "--yes", "@openai/codex"]
    return ["codex"]


PERMISSION_PARAMS: dict[str, dict[str, Any]] = {
    "never": {"approvalPolicy": "never"},
    "approve-for-me": {
        "approvalPolicy": "on-request",
        "approvalsReviewer": "auto_review",
        "sandbox": "workspace-write",
    },
    "dangerously-bypass-approvals-and-sandbox": {
        "approvalPolicy": "never",
        "sandbox": "danger-full-access",
    },
}


class JsonRpcTransport(Protocol):
    """Minimal request/notification transport used by the Codex client."""

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = 30.0,
    ) -> Any:
        ...

    async def notify(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> None:
        ...

    async def wait_notification(
        self,
        methods: str | set[str],
        *,
        predicate: Callable[[dict[str, Any]], bool] | None = None,
        timeout: float = 120.0,
    ) -> dict[str, Any]:
        ...

    async def close(self) -> None:
        ...


class ProcessJsonRpcTransport:
    """JSON-RPC over a child process's stdin/stdout."""

    def __init__(
        self,
        command: list[str] | None = None,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self._command = list(command or [*default_codex_command(), "app-server", "--stdio"])
        self._cwd = cwd
        self._env = env
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._notifications: list[dict[str, Any]] = []
        self._notification_event = asyncio.Event()
        self._write_lock = asyncio.Lock()
        self._next_id = 0
        self._closed = False
        self.stderr_tail: list[str] = []

    async def start(self) -> None:
        if self._process is not None:
            return
        try:
            self._process = await asyncio.create_subprocess_exec(
                *self._command,
                cwd=self._cwd,
                env=self._env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"Codex app-server executable not found: {self._command[0]!r}"
            ) from exc
        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = 30.0,
    ) -> Any:
        await self.start()
        self._next_id += 1
        request_id = self._next_id
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending.pop(request_id, None)

    async def notify(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> None:
        await self.start()
        await self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    async def wait_notification(
        self,
        methods: str | set[str],
        *,
        predicate: Callable[[dict[str, Any]], bool] | None = None,
        timeout: float = 120.0,
    ) -> dict[str, Any]:
        wanted = {methods} if isinstance(methods, str) else set(methods)

        async def _wait() -> dict[str, Any]:
            while True:
                for index, message in enumerate(self._notifications):
                    if message.get("method") not in wanted:
                        continue
                    params = message.get("params")
                    if not isinstance(params, dict):
                        params = {}
                    if predicate is not None and not predicate(params):
                        continue
                    self._notifications.pop(index)
                    return message
                self._notification_event.clear()
                await self._notification_event.wait()

        return await asyncio.wait_for(_wait(), timeout=timeout)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        tasks = [task for task in (self._reader_task, self._stderr_task) if task is not None]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        process = self._process
        if process is not None:
            if process.stdin is not None:
                with contextlib.suppress(Exception):
                    process.stdin.close()
                    await process.stdin.wait_closed()
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=3.0)
                except TimeoutError:
                    process.kill()
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(process.wait(), timeout=1.0)
        self._process = None
        for future in self._pending.values():
            if not future.done():
                future.set_exception(RuntimeError("Codex app-server transport closed"))
        self._pending.clear()

    async def _send(self, message: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise RuntimeError("Codex app-server transport is not running")
        data = (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")
        async with self._write_lock:
            process.stdin.write(data)
            await process.stdin.drain()

    async def _read_loop(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                try:
                    message = json.loads(line.decode("utf-8", "replace"))
                except json.JSONDecodeError:
                    continue
                if not isinstance(message, dict):
                    continue
                await self._dispatch(message)
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(RuntimeError("Codex app-server closed stdout"))

    async def _dispatch(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        method = message.get("method")
        if request_id is not None and method is None:
            future = self._pending.get(int(request_id))
            if future is None or future.done():
                return
            if "error" in message:
                future.set_exception(RuntimeError(str(message["error"])))
            else:
                future.set_result(message.get("result"))
            return

        if request_id is not None and isinstance(method, str):
            # Server-initiated request (approval, user input, ...).  The
            # unattended mode declines anything that needs a human.
            await self._send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": _unattended_response(method),
                }
            )
            return

        if isinstance(method, str):
            self._notifications.append(message)
            self._notification_event.set()

    async def _drain_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        while True:
            line = await process.stderr.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").rstrip()
            if text:
                self.stderr_tail.append(text)
                if len(self.stderr_tail) > 50:
                    del self.stderr_tail[:-50]


def _unattended_response(method: str) -> dict[str, Any]:
    if method in {
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
    }:
        return {"decision": "decline"}
    if method == "item/permissions/requestApproval":
        return {"permissions": {}, "scope": "turn"}
    if method == "item/tool/requestUserInput":
        return {"answers": {}}
    return {}


@dataclass
class CodexAppServerClient:
    """One-shot Codex app-server client."""

    transport: JsonRpcTransport
    permission_mode: str = "never"
    model: str | None = None
    protocol_version: str = "0.153.4"
    thread_id: str = ""
    _initialized: bool = field(default=False, init=False)

    async def initialize(self) -> None:
        if self._initialized:
            return
        await self.transport.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "openharness",
                    "title": "OpenHarness",
                    "version": "0.1.0",
                },
                "capabilities": {
                    "experimentalApi": False,
                    "requestAttestation": False,
                },
            },
        )
        await self.transport.notify("initialized")
        self._initialized = True

    async def start_thread(self, cwd: str) -> str:
        await self.initialize()
        params: dict[str, Any] = {
            "cwd": cwd,
            "ephemeral": True,
            **PERMISSION_PARAMS.get(self.permission_mode, PERMISSION_PARAMS["never"]),
        }
        if self.model:
            params["model"] = self.model
        response = await self.transport.request("thread/start", params)
        thread = response.get("thread") if isinstance(response, dict) else None
        thread_id = thread.get("id") if isinstance(thread, dict) else None
        if not isinstance(thread_id, str) or not thread_id:
            raise RuntimeError("Codex app-server returned no thread id")
        self.thread_id = thread_id
        return thread_id

    async def run_turn(self, text: str) -> SubagentResult:
        if not self.thread_id:
            raise RuntimeError("thread/start must run before turn/start")
        response = await self.transport.request(
            "turn/start",
            {
                "threadId": self.thread_id,
                "input": [{"type": "text", "text": text, "text_elements": []}],
            },
        )
        turn = response.get("turn") if isinstance(response, dict) else None
        turn_id = turn.get("id") if isinstance(turn, dict) else None
        if not isinstance(turn_id, str) or not turn_id:
            raise RuntimeError("Codex app-server returned no turn id")

        final_answer: str | None = None
        partial_answer: str | None = None
        unphased_answer: str | None = None
        while True:
            message = await self.transport.wait_notification(
                {"item/completed", "turn/completed"},
                predicate=lambda params: params.get("threadId") == self.thread_id
                and (
                    params.get("turnId") == turn_id
                    or isinstance(params.get("turn"), dict)
                    and params["turn"].get("id") == turn_id
                ),
            )
            if message.get("method") == "item/completed":
                item = message.get("params", {}).get("item")
                if not isinstance(item, dict) or item.get("type") != "agentMessage":
                    continue
                content = item.get("text")
                if not isinstance(content, str):
                    continue
                phase = item.get("phase")
                if phase == "final_answer":
                    final_answer = content
                elif phase == "partial_answer":
                    partial_answer = content
                elif phase is None:
                    unphased_answer = content
                continue

            completed_turn = message.get("params", {}).get("turn")
            status = (
                completed_turn.get("status")
                if isinstance(completed_turn, dict)
                else None
            )
            if status not in {"completed", "interrupted", "failed"}:
                raise RuntimeError(f"Codex turn ended with status {status!r}")
            if status != "completed":
                return SubagentResult(
                    output="",
                    diagnostic=f"Codex turn ended with status {status}",
                    stop_reason=status,
                )
            output = final_answer or partial_answer or unphased_answer or ""
            if not output.strip():
                return SubagentResult(
                    output="",
                    diagnostic="Codex completed without an answer",
                    stop_reason="invalid-result",
                )
            return SubagentResult(output=output, stop_reason="completed")

    async def run(self, prompt: str, *, cwd: str) -> SubagentResult:
        await self.start_thread(cwd)
        return await self.run_turn(prompt)

    async def close(self) -> None:
        await self.transport.close()


class CodexAppServerSubagentProvider:
    """One-shot, unattended Codex sub-agent provider."""

    name = "codex"
    inherits_parent_context = False
    capabilities = SubagentCapabilities(
        streaming=False,
        continuation=False,
        cancellation=True,
        external=True,
    )

    def __init__(
        self,
        *,
        command: list[str] | None = None,
        permission_mode: str = "never",
        model: str | None = None,
        timeout: float = 300.0,
        client_factory: Callable[..., CodexAppServerClient] | None = None,
    ) -> None:
        self._command = command
        self._permission_mode = permission_mode
        self._model = model
        self._timeout = timeout
        self._client_factory = client_factory

    async def start(self, request: SubagentStartRequest) -> SubagentRun:
        runner = asyncio.create_task(self._run(request))
        return SubagentRun(
            id=uuid.uuid4().hex,
            provider_name=self.name,
            task=request.task,
            _runner=runner,
        )

    async def _run(self, request: SubagentStartRequest) -> SubagentResult:
        client = self._make_client(request.cwd)
        try:
            return await asyncio.wait_for(
                client.run(request.task, cwd=request.cwd),
                timeout=self._timeout,
            )
        except Exception as exc:  # noqa: BLE001 - normalize provider failures
            return SubagentResult(
                output="",
                diagnostic=f"{type(exc).__name__}: {exc}",
                stop_reason="failed",
            )
        finally:
            await client.close()

    def _make_client(self, cwd: str) -> CodexAppServerClient:
        if self._client_factory is not None:
            return self._client_factory(
                permission_mode=self._permission_mode,
                model=self._model,
            )
        transport = ProcessJsonRpcTransport(self._command, cwd=cwd)
        return CodexAppServerClient(
            transport,
            permission_mode=self._permission_mode,
            model=self._model,
        )


class CodexAppServerBackend:
    """AgentBackend that delegates the whole turn to Codex app-server."""

    def __init__(
        self,
        *,
        cwd: str,
        command: list[str] | None = None,
        permission_mode: str = "never",
        model: str | None = None,
        timeout: float = 300.0,
        client_factory: Callable[..., CodexAppServerClient] | None = None,
    ) -> None:
        self._cwd = cwd
        self._command = command
        self._permission_mode = permission_mode
        self._model = model
        self._timeout = timeout
        self._client_factory = client_factory
        self._cancelled = False

    def capabilities(self) -> AgentCapabilities:
        return AgentCapabilities(
            streaming=False,
            tools=True,
            resume=False,
            cancel=True,
            approvals=False,
            native_reasoning_state=True,
        )

    async def submit(
        self,
        prompt: str | ConversationMessage,
        *,
        max_turns: int | None = None,
    ) -> AsyncIterator[StreamEvent]:
        del max_turns
        text = prompt if isinstance(prompt, str) else prompt.text
        client = self._make_client()
        try:
            result = await asyncio.wait_for(
                client.run(text, cwd=self._cwd), timeout=self._timeout
            )
        finally:
            await client.close()
        if self._cancelled:
            return
        message = ConversationMessage(
            role="assistant",
            content=[TextBlock(text=result.output)],
        )
        yield AssistantTurnComplete(message=message, usage=UsageSnapshot())

    async def resume(self, *, max_turns: int | None = None) -> AsyncIterator[StreamEvent]:
        del max_turns
        if False:
            yield AssistantTurnComplete(
                message=ConversationMessage(role="assistant"),
                usage=UsageSnapshot(),
            )

    async def cancel(self) -> None:
        self._cancelled = True

    def _make_client(self) -> CodexAppServerClient:
        if self._client_factory is not None:
            return self._client_factory(
                permission_mode=self._permission_mode,
                model=self._model,
            )
        transport = ProcessJsonRpcTransport(self._command, cwd=self._cwd)
        return CodexAppServerClient(
            transport,
            permission_mode=self._permission_mode,
            model=self._model,
        )
