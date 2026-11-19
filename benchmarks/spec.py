"""Task definitions, execution, and grading for the OpenHarness benchmark.

A task owns a prompt, a temporary workspace fixture, an optional scripted model
transcript (deterministic mode), and a grader.  Both suites share the same
runner and report format:

* ``core`` tasks run with :class:`ScriptedClient` and verify harness behaviour
  (permissions, tool execution, plan mode, output draining) without spending
  API tokens.
* ``real`` tasks run against a real model and grade the resulting workspace or
  the agent's tool trace.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openharness.api.client import ApiMessageCompleteEvent, SupportsStreamingMessages
from openharness.api.usage import UsageSnapshot
from openharness.config.paths import get_data_dir
from openharness.config.settings import Settings
from openharness.engine.events import EventSink
from openharness.engine.messages import ConversationMessage, TextBlock, ToolUseBlock
from openharness.engine.query_engine import QueryEngine
from openharness.engine.stream_events import (
    AssistantTurnComplete,
    ToolExecutionCompleted,
    ToolExecutionStarted,
)
from openharness.permissions import PermissionChecker
from openharness.prompts import build_runtime_system_prompt
from openharness.services.compact_port import ServiceCompactPort
from openharness.tools import create_default_tool_registry
from openharness.tools.base import BaseTool

PermissionPrompt = Callable[[str, str], Awaitable[bool]]
AskPrompt = Callable[..., Awaitable[str]]
Grader = Callable[["BenchRun"], list[str]]
Runner = Callable[["Task"], Awaitable["BenchRun"]]


@dataclass(frozen=True)
class ScriptedTurn:
    """One deterministic assistant response."""

    text: str = ""
    tools: tuple[tuple[str, dict[str, Any]], ...] = ()
    usage: UsageSnapshot = field(default_factory=UsageSnapshot)


@dataclass
class Task:
    """A benchmark scenario."""

    id: str
    description: str
    prompt: str
    grade: Grader
    script: tuple[ScriptedTurn, ...] = ()
    fixture: dict[str, str] = field(default_factory=dict)
    git_init: bool = False
    permission_mode: str = "default"
    # Extra PermissionSettings fields (allowed_tools, denied_tools, path_rules,
    # denied_commands, or a ``mode`` that overrides ``permission_mode``).
    permission_settings: dict[str, Any] = field(default_factory=dict)
    # Additional tools registered after the built-in registry is created.  Used
    # to inject tools that raise or that the suite otherwise needs to observe.
    extra_tools: dict[str, BaseTool] = field(default_factory=dict)
    # Optional caller-supplied sink; the runner always also records every
    # canonical event in ``BenchRun.sink_events`` for grading.
    event_sink: EventSink | None = None
    auto_compact_threshold_tokens: int | None = None
    # When positive, close the engine's async generator after this many
    # ``ToolExecutionStarted`` events, before the tool executes.
    interrupt_after_tool_starts: int = 0
    max_turns: int = 20
    # Most tasks treat an exception as a failure.  A task that probes a known
    # terminal condition (for example the max-turn cutoff) opts into grading the
    # run it did produce, using ``BenchRun.error`` as evidence.
    grade_on_error: bool = False
    timeout_seconds: float = 120.0
    # Answer used when the agent asks for confirmation. Defaults to denying so
    # a scripted mutation cannot slip through unnoticed.
    permission_reply: bool = False
    # Answer returned by the canned ask_user_question callback.
    ask_reply: str = "Use the recommended option."
    # Suites that exercise seams outside the QueryEngine boundary (provider
    # wire layer, hooks/MCP/subagents) supply their own offline runner.
    runner: Runner | None = None
    tags: tuple[str, ...] = ()


@dataclass
class BenchRun:
    """Everything a grader may inspect after one task run."""

    task: Task
    workspace: Path
    events: list[Any]
    tools: list[dict[str, Any]]
    results: list[dict[str, Any]]
    permission_prompts: list[dict[str, Any]]
    questions: list[dict[str, Any]]
    answers: list[str]
    usage: UsageSnapshot
    duration_seconds: float
    turns: int
    # Set when the engine raised or the run timed out.  Usually this is copied
    # into ``failures`` by ``run_task``; tasks with ``grade_on_error`` inspect
    # it instead so an expected terminal condition can still pass.
    error: str | None = None
    # Canonical ``AgentEvent`` objects delivered to the engine's event sink.
    # ``turn.end`` only travels here, never through the generator.
    sink_events: list[Any] = field(default_factory=list)
    # Durable session messages when the run finished or was interrupted.
    messages: list[ConversationMessage] = field(default_factory=list)
    # Per-task ``OPENHARNESS_DATA_DIR`` and the host data dir it replaced.
    data_dir: Path | None = None
    host_data_dir: Path | None = None
    # Suite-specific observations for runners that do not use the agent loop.
    details: dict[str, Any] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.failures

    @property
    def tool_names(self) -> list[str]:
        return [str(item["name"]) for item in self.tools]

    def final_text(self) -> str:
        texts = [
            event.message.text
            for event in self.events
            if isinstance(event, AssistantTurnComplete) and event.message.text.strip()
        ]
        return texts[-1] if texts else ""


_COMPACTION_SYSTEM_MARKERS = ("conversation summarizer",)
_COMPACTION_PROMPT_MARKERS = (
    "detailed summary of the conversation",
    "<summary>",
)
_COMPACTION_REPLY = "Benchmark compaction summary: earlier context was condensed."


def _is_compaction_request(request: Any) -> bool:
    """Return True for the summarizer call issued by auto-compaction.

    The runtime reuses the agent's api_client for compaction.  With a non-empty
    agent system prompt the literal "conversation summarizer" default is never
    sent, so also recognise the dedicated no-tools summarizer prompt.
    """
    system_prompt = str(getattr(request, "system_prompt", "") or "").lower()
    if any(marker in system_prompt for marker in _COMPACTION_SYSTEM_MARKERS):
        return True
    messages = getattr(request, "messages", ()) or ()
    if not messages or getattr(request, "tools", None):
        return False
    last_text = str(getattr(messages[-1], "text", "") or "").lower()
    return any(marker in last_text for marker in _COMPACTION_PROMPT_MARKERS)


class CollectingEventSink:
    """Keep every canonical engine event so a grader can inspect it."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, event: Any) -> None:
        self.events.append(event)

    async def close(self) -> None:
        return None


class _TeeEventSink:
    """Fan an event out to several sinks in order."""

    def __init__(self, sinks: Sequence[EventSink]) -> None:
        self._sinks = list(sinks)

    async def emit(self, event: Any) -> None:
        for sink in self._sinks:
            await sink.emit(event)

    async def close(self) -> None:
        for sink in self._sinks:
            await sink.close()


class ScriptedClient:
    """Deterministic streaming client that replays a fixed transcript."""

    def __init__(self, turns: Sequence[ScriptedTurn]) -> None:
        self._turns = list(turns)
        self._calls = 0

    async def stream_message(self, request):
        # Compaction reuses this client.  Answer it with a fixed summary and do
        # not consume a scripted turn, or every later turn shifts by one.
        if _is_compaction_request(request):
            yield ApiMessageCompleteEvent(
                message=ConversationMessage(
                    role="assistant",
                    content=[TextBlock(text=_COMPACTION_REPLY)],
                ),
                usage=UsageSnapshot(),
                stop_reason=None,
            )
            return

        index = self._calls
        self._calls += 1
        turn = (
            self._turns[index]
            if index < len(self._turns)
            else ScriptedTurn(text="Benchmark script complete.")
        )

        content: list[Any] = []
        if turn.text:
            content.append(TextBlock(text=turn.text))
        for offset, (name, payload) in enumerate(turn.tools):
            content.append(
                ToolUseBlock(
                    id=f"bench_{index}_{offset}",
                    name=name,
                    input=dict(payload),
                )
            )

        yield ApiMessageCompleteEvent(
            message=ConversationMessage(role="assistant", content=content),
            usage=turn.usage,
            stop_reason=None,
        )


def _materialize_script(turns: Sequence[ScriptedTurn], workspace: Path) -> tuple[ScriptedTurn, ...]:
    # Bash is the target shell, so use forward slashes even on Windows.
    root = workspace.as_posix()

    def substitute(value: Any) -> Any:
        return value.replace("{workspace}", root) if isinstance(value, str) else value

    return tuple(
        ScriptedTurn(
            text=turn.text.replace("{workspace}", root),
            tools=tuple(
                (name, {key: substitute(value) for key, value in payload.items()})
                for name, payload in turn.tools
            ),
            usage=turn.usage,
        )
        for turn in turns
    )


def _git_init(workspace: Path) -> None:
    commands = (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "bench@example.com"],
        ["git", "config", "user.name", "Benchmark"],
        ["git", "add", "-A"],
        ["git", "commit", "-qm", "initial"],
    )
    for command in commands:
        subprocess.run(command, cwd=workspace, capture_output=True, check=False, timeout=30)


def _build_engine(
    task: Task,
    client: SupportsStreamingMessages,
    workspace: Path,
    *,
    model: str,
    permission_prompts: list[dict[str, Any]],
    questions: list[dict[str, Any]],
    answers: list[str],
    event_sink: EventSink,
) -> QueryEngine:
    permission: dict[str, Any] = {"mode": task.permission_mode}
    permission.update(task.permission_settings)
    settings = Settings(
        model=model,
        max_turns=task.max_turns,
        permission=permission,
        # The harness owns no durable state in scripted runs; keep memory
        # services away from the real ``~/.openharness`` directory.
        memory={"enabled": False},
    )
    system_prompt = build_runtime_system_prompt(
        settings,
        cwd=workspace,
        latest_user_prompt=task.prompt,
        include_project_memory=False,
    )

    async def permission_prompt(tool_name: str, reason: str) -> bool:
        permission_prompts.append({"tool": tool_name, "reason": reason})
        return task.permission_reply

    async def ask_user_prompt(question: str, options: list[str] | None = None) -> str:
        questions.append({"question": question, "options": list(options or [])})
        answers.append(task.ask_reply)
        return task.ask_reply

    registry = create_default_tool_registry()
    for tool in task.extra_tools.values():
        registry.register(tool)

    return QueryEngine(
        api_client=client,
        tool_registry=registry,
        permission_checker=PermissionChecker(settings.permission),
        cwd=workspace,
        model=model,
        system_prompt=system_prompt,
        max_tokens=4096,
        max_turns=task.max_turns,
        permission_prompt=permission_prompt,
        ask_user_prompt=ask_user_prompt,
        settings=settings,
        event_sink=event_sink,
        auto_compact_threshold_tokens=task.auto_compact_threshold_tokens,
        compact_port=ServiceCompactPort(),
    )


async def _collect_events(
    engine: QueryEngine,
    prompt: str,
    sink: list[Any],
    *,
    interrupt_after_tool_starts: int = 0,
) -> None:
    """Drain the engine, optionally closing it after N tool-start events.

    ``aclose`` runs the generator's ``finally`` block, which is what emits the
    ``turn.end`` sink event with status ``interrupted``.  Closing here happens
    before the tool body executes, so no partial tool result is committed.
    """
    generator = engine.submit_message(prompt)
    seen_tool_starts = 0
    try:
        while True:
            try:
                event = await generator.__anext__()
            except StopAsyncIteration:
                return
            sink.append(event)
            if not isinstance(event, ToolExecutionStarted):
                continue
            seen_tool_starts += 1
            if (
                interrupt_after_tool_starts
                and seen_tool_starts >= interrupt_after_tool_starts
            ):
                return
    finally:
        await generator.aclose()


async def run_task(
    task: Task,
    *,
    scripted: bool,
    client: SupportsStreamingMessages | None = None,
    model: str = "scripted",
) -> BenchRun:
    """Create the fixture workspace, run the prompt, and grade the outcome."""
    # External ids such as ``HumanEval/0`` cannot appear in a Windows path.
    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", task.id)
    with tempfile.TemporaryDirectory(prefix=f"oh-bench-{safe_id}-") as raw:
        workspace = Path(raw)
        for relative, content in task.fixture.items():
            path = workspace / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        if task.git_init:
            _git_init(workspace)

        if scripted:
            active_client: SupportsStreamingMessages = ScriptedClient(
                _materialize_script(task.script, workspace)
            )
        else:
            if client is None:
                raise ValueError("real benchmark tasks require an API client")
            active_client = client

        # Keep artifact/output writes inside this task's temporary directory.
        # ``tool_artifact_dir`` re-reads the env var on every call.
        host_data_dir = get_data_dir()
        task_data_dir = workspace / "data"
        previous_data_dir = os.environ.get("OPENHARNESS_DATA_DIR")
        os.environ["OPENHARNESS_DATA_DIR"] = str(task_data_dir)
        try:
            return await _execute_run(
                task,
                active_client,
                workspace,
                model=model,
                task_data_dir=task_data_dir,
                host_data_dir=host_data_dir,
            )
        finally:
            if previous_data_dir is None:
                os.environ.pop("OPENHARNESS_DATA_DIR", None)
            else:
                os.environ["OPENHARNESS_DATA_DIR"] = previous_data_dir


async def _execute_run(
    task: Task,
    active_client: SupportsStreamingMessages,
    workspace: Path,
    *,
    model: str,
    task_data_dir: Path,
    host_data_dir: Path,
) -> BenchRun:
    permission_prompts: list[dict[str, Any]] = []
    questions: list[dict[str, Any]] = []
    answers: list[str] = []
    collector = CollectingEventSink()
    event_sink: EventSink = (
        collector
        if task.event_sink is None
        else _TeeEventSink([task.event_sink, collector])
    )
    engine = _build_engine(
        task,
        active_client,
        workspace,
        model=model,
        permission_prompts=permission_prompts,
        questions=questions,
        answers=answers,
        event_sink=event_sink,
    )

    events: list[Any] = []
    error: str | None = None
    started = time.monotonic()
    try:
        await asyncio.wait_for(
            _collect_events(
                engine,
                task.prompt,
                events,
                interrupt_after_tool_starts=task.interrupt_after_tool_starts,
            ),
            timeout=task.timeout_seconds,
        )
    except TimeoutError:
        error = f"timed out after {task.timeout_seconds:g}s"
    except Exception as exc:  # noqa: BLE001 - failures are benchmark data
        error = f"{type(exc).__name__}: {exc}"
    duration = time.monotonic() - started

    tools = [
        {"name": event.tool_name, "input": dict(event.tool_input)}
        for event in events
        if isinstance(event, ToolExecutionStarted)
    ]
    results = [
        {"name": event.tool_name, "ok": not event.is_error, "output": event.output}
        for event in events
        if isinstance(event, ToolExecutionCompleted)
    ]
    turns = sum(1 for event in events if isinstance(event, AssistantTurnComplete))

    run = BenchRun(
        task=task,
        workspace=workspace,
        events=events,
        tools=tools,
        results=results,
        permission_prompts=permission_prompts,
        questions=questions,
        answers=answers,
        usage=engine.total_usage,
        duration_seconds=duration,
        turns=turns,
        error=error,
        sink_events=list(collector.events),
        messages=engine.messages,
        data_dir=task_data_dir,
        host_data_dir=host_data_dir,
    )
    return grade_run(task, run)


def grade_run(task: Task, run: BenchRun) -> BenchRun:
    """Apply *task*'s grader, turning grader bugs into benchmark failures."""
    if run.error is not None and not task.grade_on_error:
        run.failures.append(run.error)
    else:
        try:
            run.failures.extend(task.grade(run))
        except Exception as exc:  # noqa: BLE001 - grader bugs become failures
            run.failures.append(f"grader raised {type(exc).__name__}: {exc}")
    return run


# ---------------------------------------------------------------------------
# Shared grading helpers
# ---------------------------------------------------------------------------


def expect_tool(run: BenchRun, name: str, *, at_least: int = 1) -> list[str]:
    count = run.tool_names.count(name)
    if count >= at_least:
        return []
    return [f"expected at least {at_least} {name} call(s), saw {count}"]


def expect_tool_ok(run: BenchRun, name: str) -> list[str]:
    matching = [result for result in run.results if result["name"] == name]
    if not matching:
        return [f"expected a {name} result, saw none"]
    failed = [result for result in matching if not result["ok"]]
    if failed:
        return [f"{name} failed: {failed[-1]['output'][:500]}"]
    return []


def expect_no_permission_prompts(run: BenchRun) -> list[str]:
    if not run.permission_prompts:
        return []
    return [f"unexpected permission prompt(s): {run.permission_prompts}"]


def expect_permission_prompt(run: BenchRun, tool_name: str = "bash") -> list[str]:
    if any(prompt["tool"] == tool_name for prompt in run.permission_prompts):
        return []
    return [f"expected a permission prompt for {tool_name}, saw {run.permission_prompts}"]


def run_pytest(workspace: Path, *, timeout: float = 90.0) -> tuple[bool, str]:
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    output = (completed.stdout + completed.stderr).strip()
    return completed.returncode == 0, output[-2000:]


__all__ = [
    "AskPrompt",
    "BenchRun",
    "Grader",
    "PermissionPrompt",
    "Runner",
    "ScriptedClient",
    "ScriptedTurn",
    "Task",
    "expect_no_permission_prompts",
    "expect_permission_prompt",
    "expect_tool",
    "expect_tool_ok",
    "grade_run",
    "run_pytest",
    "run_task",
]
