"""Byte-level prompt-cache stability gates (ADR-0010 D7 / ADR-0011 I7).

These tests pin the invariants that keep the provider's prefix cache warm:

1. the stable system prefix and tool schema do not depend on cwd/date/branch;
2. activating a deferred tool only appends to the tool prefix;
3. a memory write changes the turn tail, never the stable prefix or history;
4. ``continue_pending`` uses the same stable prefix as a normal turn.

They deliberately use a recording fake rather than a real provider: the
assertion is about the bytes we send, not about provider behavior.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from pydantic import BaseModel

from openharness.api.client import ApiMessageCompleteEvent
from openharness.api.usage import UsageSnapshot
from openharness.config.settings import MemorySettings, Settings
from openharness.engine.messages import ConversationMessage, TextBlock
from openharness.engine.query_engine import QueryEngine
from openharness.memory.paths import get_project_memory_dir
from openharness.permissions.checker import PermissionChecker
from openharness.prompts import system_prompt as system_prompt_module
from openharness.prompts.context import (
    build_stable_system_prompt_result,
    build_turn_context,
)
from openharness.prompts.environment import EnvironmentInfo
from openharness.tools.base import BaseTool, ToolRegistry, ToolResult


class _RecordingClient:
    """Capture each request so tests can compare the exact wire shape."""

    def __init__(self, text: str = "ok") -> None:
        self.requests = []
        self._text = text

    async def stream_message(self, request):
        self.requests.append(
            dataclasses.replace(request, messages=list(request.messages))
        )
        yield ApiMessageCompleteEvent(
            message=ConversationMessage(
                role="assistant", content=[TextBlock(text=self._text)]
            ),
            usage=UsageSnapshot(input_tokens=10, output_tokens=1),
            stop_reason="stop",
        )


class _EmptyInput(BaseModel):
    pass


class _NamedTool(BaseTool):
    input_model = _EmptyInput

    def __init__(self, name: str) -> None:
        self.name = name
        self.description = f"{name} tool"

    async def execute(self, arguments, context) -> ToolResult:
        return ToolResult(output="")


def _registry(*names: str) -> ToolRegistry:
    registry = ToolRegistry()
    for name in names:
        registry.register(_NamedTool(name))
    return registry


def _engine(
    tmp_path: Path,
    client: _RecordingClient,
    *,
    registry: ToolRegistry | None = None,
    settings: Settings | None = None,
    turn_context_builder=None,
    system_prompt: str = "STABLE SYSTEM",
    cacheable_prefix: str = "STABLE SYSTEM",
    section_hashes: tuple[tuple[str, str], ...] = (),
) -> QueryEngine:
    return QueryEngine(
        api_client=client,
        tool_registry=registry or ToolRegistry(),
        permission_checker=PermissionChecker(Settings().permission),
        cwd=tmp_path,
        model="deepseek-chat",
        system_prompt=system_prompt,
        system_prompt_cacheable_prefix=cacheable_prefix,
        system_prompt_section_hashes=section_hashes,
        settings=settings,
        turn_context_builder=turn_context_builder,
    )


def _env(*, cwd: str, date: str, branch: str) -> EnvironmentInfo:
    return EnvironmentInfo(
        os_name="TestOS",
        os_version="1",
        platform_machine="x86_64",
        shell="bash",
        cwd=cwd,
        home_dir="/home/tester",
        date=date,
        python_version="3.12",
        python_executable="/usr/bin/python",
        virtual_env=None,
        is_git_repo=True,
        git_branch=branch,
        hostname="tester",
    )


@pytest.fixture(autouse=True)
def _clear_coordinator_mode(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_COORDINATOR_MODE", raising=False)


@pytest.mark.asyncio
async def test_stable_prefix_ignores_cwd_clock_and_branch(tmp_path: Path, monkeypatch):
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()

    current = {"env": _env(cwd=str(first_dir), date="2026-01-01", branch="main")}
    monkeypatch.setattr(
        system_prompt_module, "get_environment_info", lambda cwd=None: current["env"]
    )

    settings = Settings()
    first = build_stable_system_prompt_result(
        settings, cwd=first_dir, available_tools={"read_file"}
    )
    current["env"] = _env(cwd=str(second_dir), date="2027-12-31", branch="feature")
    second = build_stable_system_prompt_result(
        settings, cwd=second_dir, available_tools={"read_file"}
    )

    # cwd/date/branch belong to the turn context, not the cached prefix.
    assert first.cacheable_prefix == second.cacheable_prefix
    assert first.text == second.text
    assert first.section_hashes == second.section_hashes

    # Two turns through the engine see the same stable hash and tool hash, and
    # the second request's message list extends the first as a prefix.
    client = _RecordingClient()
    engine = _engine(
        tmp_path,
        client,
        system_prompt=first.text,
        cacheable_prefix=first.cacheable_prefix,
        section_hashes=first.section_hashes,
    )
    [event async for event in engine.submit_message("first")]
    first_fp = engine.cache_diagnostics.last_fingerprint
    [event async for event in engine.submit_message("second")]
    second_fp = engine.cache_diagnostics.last_fingerprint

    assert first_fp is not None and second_fp is not None
    assert first_fp.stable_sha == second_fp.stable_sha
    assert first_fp.tools_sha == second_fp.tools_sha
    assert second_fp.message_hashes[: len(first_fp.message_hashes)] == (
        first_fp.message_hashes
    )


@pytest.mark.asyncio
async def test_activate_tools_only_appends_to_the_cached_tool_prefix(tmp_path: Path):
    client = _RecordingClient()
    engine = _engine(
        tmp_path,
        client,
        registry=_registry("bash", "tool_search", "notebook_edit"),
        settings=Settings(tool_profile="core"),
    )

    [event async for event in engine.submit_message("first")]
    before = [item["name"] for item in client.requests[0].tools]

    engine.activate_tools(["notebook_edit"])
    [event async for event in engine.submit_message("second")]
    after = [item["name"] for item in client.requests[1].tools]

    assert before == ["bash", "tool_search"]
    assert after[: len(before)] == before
    assert after == ["bash", "tool_search", "notebook_edit"]


@pytest.mark.asyncio
async def test_memory_write_only_changes_the_turn_tail(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    settings = Settings(memory=MemorySettings(session_memory_enabled=False))

    def _turn_context(prompt: str, metadata: dict[str, object]) -> str:
        del metadata
        return build_turn_context(
            settings,
            cwd=tmp_path,
            latest_user_prompt=prompt,
        )

    client = _RecordingClient()
    engine = _engine(
        tmp_path,
        client,
        settings=settings,
        turn_context_builder=_turn_context,
    )

    [event async for event in engine.submit_message("first")]
    first_fp = engine.cache_diagnostics.last_fingerprint
    assert first_fp is not None

    memory_entry = get_project_memory_dir(tmp_path) / "MEMORY.md"
    memory_entry.write_text("# Memory\n\nremember X\n", encoding="utf-8")

    [event async for event in engine.submit_message("second")]
    second_fp = engine.cache_diagnostics.last_fingerprint
    assert second_fp is not None

    # Memory lives in the turn context (a tail message), so a write must not
    # drift the stable prefix or rewrite history.
    assert first_fp.stable_sha == second_fp.stable_sha
    assert first_fp.tools_sha == second_fp.tools_sha
    assert second_fp.message_hashes[: len(first_fp.message_hashes)] == (
        first_fp.message_hashes
    )
    summary = engine.cache_diagnostics.summary()
    assert summary["system_drifts"] == 0
    assert summary["tool_drifts"] == 0
    assert summary["history_rewrites"] == 0
    assert summary["stable_sha"] == second_fp.stable_sha
    assert summary["tools_sha"] == second_fp.tools_sha

    assert "remember X" not in client.requests[1].system_prompt
    assert any(
        isinstance(block, TextBlock) and "remember X" in block.text
        for message in client.requests[1].messages
        for block in message.content
    )


@pytest.mark.asyncio
async def test_continue_pending_keeps_the_same_stable_prefix(tmp_path: Path):
    normal_client = _RecordingClient()
    normal = _engine(tmp_path, normal_client)
    [event async for event in normal.submit_message("first")]
    normal_first = normal.cache_diagnostics.last_fingerprint
    [event async for event in normal.submit_message("second")]
    normal_second = normal.cache_diagnostics.last_fingerprint

    continuation_client = _RecordingClient()
    continuation = _engine(tmp_path, continuation_client)
    [event async for event in continuation.submit_message("first")]
    continuation_first = continuation.cache_diagnostics.last_fingerprint
    [event async for event in continuation.continue_pending()]
    continuation_next = continuation.cache_diagnostics.last_fingerprint

    assert normal_first is not None
    assert normal_second is not None
    assert continuation_first is not None
    assert continuation_next is not None

    stable_hashes = {
        normal_first.stable_sha,
        normal_second.stable_sha,
        continuation_first.stable_sha,
        continuation_next.stable_sha,
    }
    tool_hashes = {
        normal_first.tools_sha,
        normal_second.tools_sha,
        continuation_first.tools_sha,
        continuation_next.tools_sha,
    }
    assert len(stable_hashes) == 1
    assert len(tool_hashes) == 1

    # Both paths extend the same first-turn message prefix; neither rewrites it.
    assert normal_second.message_hashes[: len(normal_first.message_hashes)] == (
        normal_first.message_hashes
    )
    assert continuation_next.message_hashes[
        : len(continuation_first.message_hashes)
    ] == continuation_first.message_hashes
    assert normal_first.message_hashes == continuation_first.message_hashes
