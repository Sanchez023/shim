"""Tests for the React backend host protocol."""

from __future__ import annotations

import asyncio
import io
import json
import sys
from types import SimpleNamespace

import pytest

from openharness.api.client import ApiMessageCompleteEvent, ApiTextDeltaEvent, ApiThinkingDeltaEvent
from openharness.api.usage import UsageSnapshot
from openharness.config.settings import Settings, save_settings
from openharness.engine.messages import (
    ConversationMessage,
    ImageBlock,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
)
from openharness.engine.stream_events import CompactProgressEvent
from openharness.tasks import get_task_manager
from openharness.ui.backend_host import (
    BackendHostConfig,
    ReactBackendHost,
    _build_user_message_with_images,
    _format_transcript_line,
    run_backend_host,
)
from openharness.ui.protocol import BackendEvent, FrontendImageAttachment, FrontendRequest
from openharness.ui.runtime import build_runtime, close_runtime, start_runtime


class StaticApiClient:
    """Fake streaming client for backend host tests."""

    def __init__(self, text: str) -> None:
        self._text = text

    async def stream_message(self, request):
        del request
        yield ApiMessageCompleteEvent(
            message=ConversationMessage(role="assistant", content=[TextBlock(text=self._text)]),
            usage=UsageSnapshot(input_tokens=2, output_tokens=3),
            stop_reason=None,
        )


class ToolThenAnswerApiClient:
    """First step reads a file, second step answers."""

    def __init__(self, path: str) -> None:
        self._path = path
        self.calls = 0

    async def stream_message(self, request):
        del request
        self.calls += 1
        if self.calls == 1:
            yield ApiMessageCompleteEvent(
                message=ConversationMessage(
                    role="assistant",
                    content=[
                        ToolUseBlock(id="toolu_read", name="read_file", input={"path": self._path})
                    ],
                ),
                usage=UsageSnapshot(input_tokens=200_000, output_tokens=10),
                stop_reason="tool_use",
            )
            return
        yield ApiMessageCompleteEvent(
            message=ConversationMessage(
                role="assistant", content=[TextBlock(text="finished")]
            ),
            usage=UsageSnapshot(input_tokens=200_000, output_tokens=10),
            stop_reason="end_turn",
        )


class ThinkingApiClient:
    """Fake client that streams reasoning before the answer."""

    async def stream_message(self, request):
        del request
        yield ApiThinkingDeltaEvent(text="weighing options. ")
        yield ApiTextDeltaEvent(text="answer")
        yield ApiMessageCompleteEvent(
            message=ConversationMessage(
                role="assistant",
                content=[
                    ThinkingBlock(thinking="weighing options.", signature="sig"),
                    TextBlock(text="answer"),
                ],
            ),
            usage=UsageSnapshot(input_tokens=2, output_tokens=3),
            stop_reason=None,
        )


class CapturingApiClient(StaticApiClient):
    """Fake streaming client that records model requests."""

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.requests = []

    async def stream_message(self, request):
        self.requests.append(request)
        async for event in super().stream_message(request):
            yield event


class FailingApiClient:
    """Fake client that triggers the query-loop ErrorEvent path."""

    def __init__(self, message: str) -> None:
        self._message = message

    async def stream_message(self, request):
        del request
        if False:
            yield None
        raise RuntimeError(self._message)


class FakeBinaryStdout:
    """Capture protocol writes through a binary stdout buffer."""

    def __init__(self) -> None:
        self.buffer = io.BytesIO()

    def flush(self) -> None:
        return None


def test_frontend_request_accepts_image_attachments():
    request = FrontendRequest.model_validate(
        {
            "type": "submit_line",
            "line": "describe this",
            "images": [
                {
                    "media_type": "image/png",
                    "data": "aGVsbG8=",
                    "source_path": "clipboard:clipboard.png",
                }
            ],
        }
    )

    assert request.images[0].media_type == "image/png"
    assert request.images[0].data == "aGVsbG8="


def test_frontend_request_rejects_non_image_attachments():
    with pytest.raises(ValueError):
        FrontendRequest.model_validate(
            {
                "type": "submit_line",
                "images": [
                    {
                        "media_type": "text/plain",
                        "data": "aGVsbG8=",
                    }
                ],
            }
        )


def test_build_user_message_with_images():
    message = _build_user_message_with_images(
        "What is in this screenshot?",
        [
            FrontendImageAttachment(
                media_type="image/png",
                data="aGVsbG8=",
                source_path="clipboard:clipboard.png",
            )
        ],
    )

    assert message is not None
    assert message.text == "What is in this screenshot?"
    assert isinstance(message.content[1], ImageBlock)
    assert message.content[1].media_type == "image/png"
    assert message.content[1].source_path == "clipboard:clipboard.png"


def test_format_transcript_line_mentions_attached_images():
    assert _format_transcript_line("inspect", []) == "inspect"
    assert _format_transcript_line("", [FrontendImageAttachment(media_type="image/png", data="x")]) == "[1 image attached]"
    assert _format_transcript_line(
        "inspect",
        [
            FrontendImageAttachment(media_type="image/png", data="x"),
            FrontendImageAttachment(media_type="image/jpeg", data="y"),
        ],
    ) == "inspect\n[2 images attached]"


@pytest.mark.asyncio
async def test_run_backend_host_accepts_permission_mode(monkeypatch):
    captured: dict[str, str | None] = {}

    async def _fake_run(self):
        captured["permission_mode"] = self._config.permission_mode
        return 0

    monkeypatch.setattr("openharness.ui.backend_host.ReactBackendHost.run", _fake_run)

    result = await run_backend_host(
        api_client=StaticApiClient("unused"),
        permission_mode="full_auto",
    )

    assert result == 0
    assert captured["permission_mode"] == "full_auto"


@pytest.mark.asyncio
async def test_read_requests_resolves_permission_response_without_queueing(monkeypatch):
    host = ReactBackendHost(BackendHostConfig(api_client=StaticApiClient("unused")))
    fut = asyncio.get_running_loop().create_future()
    host._permission_requests["req-1"] = fut

    payload = b'{"type":"permission_response","request_id":"req-1","allowed":true}\n'

    class _FakeBuffer:
        def __init__(self):
            self._reads = 0

        def readline(self):
            self._reads += 1
            if self._reads == 1:
                return payload
            return b""

    class _FakeStdin:
        buffer = _FakeBuffer()

    monkeypatch.setattr("openharness.ui.backend_host.sys.stdin", _FakeStdin())

    await host._read_requests()

    assert fut.done()
    assert fut.result() is True
    queued = await host._request_queue.get()
    assert queued.type == "shutdown"
    assert host._request_queue.empty()


@pytest.mark.asyncio
async def test_read_requests_resolves_edit_approval_response_without_queueing(monkeypatch):
    host = ReactBackendHost(BackendHostConfig(api_client=StaticApiClient("unused")))
    fut = asyncio.get_running_loop().create_future()
    host._edit_approval_requests["req-1"] = fut

    payload = b'{"type":"permission_response","request_id":"req-1","allowed":true,"permission_reply":"always"}\n'

    class _FakeBuffer:
        def __init__(self):
            self._reads = 0

        def readline(self):
            self._reads += 1
            if self._reads == 1:
                return payload
            return b""

    class _FakeStdin:
        buffer = _FakeBuffer()

    monkeypatch.setattr("openharness.ui.backend_host.sys.stdin", _FakeStdin())

    await host._read_requests()

    assert fut.done()
    assert fut.result() == "always"
    queued = await host._request_queue.get()
    assert queued.type == "shutdown"
    assert host._request_queue.empty()


@pytest.mark.asyncio
async def test_read_requests_interrupt_cancels_active_request(monkeypatch):
    host = ReactBackendHost(BackendHostConfig(api_client=StaticApiClient("unused")))

    async def _long_running():
        await asyncio.Event().wait()

    task = asyncio.create_task(_long_running())
    host._active_request_task = task

    class _FakeBuffer:
        def __init__(self):
            self._reads = 0

        def readline(self):
            self._reads += 1
            if self._reads == 1:
                return b'{"type":"interrupt"}\n'
            return b""

    class _FakeStdin:
        buffer = _FakeBuffer()

    monkeypatch.setattr("openharness.ui.backend_host.sys.stdin", _FakeStdin())

    await host._read_requests()

    with pytest.raises(asyncio.CancelledError):
        await task
    queued = await host._request_queue.get()
    assert queued.type == "shutdown"


@pytest.mark.asyncio
async def test_run_active_request_recovers_from_cancel(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))

    host = ReactBackendHost(BackendHostConfig(api_client=StaticApiClient("unused")))
    host._bundle = await build_runtime(api_client=StaticApiClient("unused"))
    events = []

    async def _emit(event):
        events.append(event)

    host._emit = _emit  # type: ignore[method-assign]
    await start_runtime(host._bundle)

    async def _long_running():
        await asyncio.Event().wait()
        return True

    try:
        runner = asyncio.create_task(host._run_active_request(_long_running()))
        while host._active_request_task is None:
            await asyncio.sleep(0)
        await host._interrupt_active_request()
        assert await runner is True
    finally:
        await close_runtime(host._bundle)

    assert any(
        event.type == "transcript_item"
        and event.item
        and event.item.role == "system"
        and "Interrupted" in event.item.text
        for event in events
    )
    assert any(event.type == "line_complete" for event in events)


@pytest.mark.asyncio
async def test_backend_host_emits_thinking_delta_before_answer(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))

    host = ReactBackendHost(BackendHostConfig(api_client=ThinkingApiClient()))
    host._bundle = await build_runtime(api_client=ThinkingApiClient())
    events = []

    async def _emit(event):
        events.append(event)

    host._emit = _emit  # type: ignore[method-assign]
    await start_runtime(host._bundle)
    try:
        await host._process_line("hi")
    finally:
        await close_runtime(host._bundle)

    thinking_index = next(
        index for index, event in enumerate(events) if event.type == "thinking_delta"
    )
    answer_index = next(
        index for index, event in enumerate(events) if event.type == "assistant_complete"
    )
    assert thinking_index < answer_index
    assert events[thinking_index].message == "weighing options. "


@pytest.mark.asyncio
async def test_backend_host_processes_command(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))

    host = ReactBackendHost(BackendHostConfig(api_client=StaticApiClient("unused")))
    host._bundle = await build_runtime(api_client=StaticApiClient("unused"))
    events = []

    async def _emit(event):
        events.append(event)

    host._emit = _emit  # type: ignore[method-assign]
    await start_runtime(host._bundle)
    try:
        should_continue = await host._process_line("/version")
    finally:
        await close_runtime(host._bundle)

    assert should_continue is True
    assert any(event.type == "transcript_item" and event.item and event.item.role == "user" for event in events)
    assert any(
        event.type == "transcript_item"
        and event.item
        and event.item.role == "system"
        and "OpenHarness" in event.item.text
        for event in events
    )
    assert any(event.type == "state_snapshot" for event in events)


@pytest.mark.asyncio
async def test_backend_host_processes_model_turn(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))

    host = ReactBackendHost(BackendHostConfig(api_client=StaticApiClient("hello from react backend")))
    host._bundle = await build_runtime(api_client=StaticApiClient("hello from react backend"))
    events = []

    async def _emit(event):
        events.append(event)

    host._emit = _emit  # type: ignore[method-assign]
    await start_runtime(host._bundle)
    try:
        should_continue = await host._process_line("hi")
    finally:
        await close_runtime(host._bundle)

    assert should_continue is True
    assert any(
        event.type == "assistant_complete" and event.message == "hello from react backend"
        for event in events
    )
    assert any(
        event.type == "assistant_complete"
        and event.item
        and event.item.role == "assistant"
        and "hello from react backend" in event.item.text
        for event in events
    )


@pytest.mark.asyncio
async def test_status_snapshot_reflects_tool_results_before_the_next_model_turn(
    tmp_path, monkeypatch
):
    """The ctx meter must move when a tool result joins the history.

    A running turn keeps its messages in a working list, so snapshots built
    from ``engine.messages`` used to ignore the tool result until the turn
    committed.  The UI then showed a stale value for the whole next model
    round-trip (all streaming and thinking time).
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    notes = tmp_path / "notes.txt"
    notes.write_text("alpha bravo charlie delta " * 1200, encoding="utf-8")

    client = ToolThenAnswerApiClient(str(notes))
    host = ReactBackendHost(
        BackendHostConfig(api_client=client, permission_mode="full_auto")
    )
    host._bundle = await build_runtime(
        api_client=client,
        permission_mode="full_auto",
    )
    events = []

    async def _emit(event):
        events.append(event)

    host._emit = _emit  # type: ignore[method-assign]
    await start_runtime(host._bundle)
    try:
        await host._process_line("read the notes")
    finally:
        await close_runtime(host._bundle)

    def context_tokens(index: int) -> int:
        return int((events[index].state or {}).get("context_tokens") or 0)

    tool_index = next(
        index for index, event in enumerate(events) if event.type == "tool_completed"
    )
    answer_index = next(
        index
        for index, event in enumerate(events)
        if index > tool_index and event.type == "assistant_complete"
    )
    refreshes = [
        index
        for index, event in enumerate(events)
        if tool_index < index < answer_index and event.type == "state_snapshot"
    ]
    assert refreshes, "expected a ctx refresh after the tool result was appended"

    before = max(
        (
            context_tokens(index)
            for index, event in enumerate(events)
            if index < tool_index and event.type == "state_snapshot"
        ),
        default=0,
    )
    # The refresh that matters is the one once the result is in the history,
    # not the pre-append snapshot emitted alongside tool_completed.
    assert context_tokens(refreshes[-1]) > before + 500


@pytest.mark.asyncio
async def test_tool_completed_carries_the_edit_diff_metadata(tmp_path, monkeypatch):
    """The TUI renders the edit diff from the tool result, not by re-reading files."""

    class _WriteClient:
        def __init__(self) -> None:
            self.calls = 0

        async def stream_message(self, request):
            del request
            self.calls += 1
            if self.calls == 1:
                yield ApiMessageCompleteEvent(
                    message=ConversationMessage(
                        role="assistant",
                        content=[
                            ToolUseBlock(
                                id="toolu_write",
                                name="write_file",
                                input={"path": "out.txt", "content": "alpha\nbeta\n"},
                            )
                        ],
                    ),
                    usage=UsageSnapshot(input_tokens=1, output_tokens=1),
                    stop_reason="tool_use",
                )
                return
            yield ApiMessageCompleteEvent(
                message=ConversationMessage(
                    role="assistant", content=[TextBlock(text="done")]
                ),
                usage=UsageSnapshot(input_tokens=1, output_tokens=1),
                stop_reason="end_turn",
            )

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))

    client = _WriteClient()
    host = ReactBackendHost(
        BackendHostConfig(api_client=client, permission_mode="full_auto")
    )
    host._bundle = await build_runtime(
        api_client=client,
        permission_mode="full_auto",
        cwd=str(tmp_path),
    )
    events = []

    async def _emit(event):
        events.append(event)

    host._emit = _emit  # type: ignore[method-assign]
    await start_runtime(host._bundle)
    try:
        await host._process_line("write the file")
    finally:
        await close_runtime(host._bundle)

    completed = next(
        event
        for event in events
        if event.type == "tool_completed" and event.tool_name == "write_file"
    )
    assert completed.item is not None
    assert completed.item.metadata is not None
    assert completed.item.metadata["diff_added"] == 2
    assert completed.item.metadata["diff_removed"] == 0
    assert "+alpha" in completed.item.metadata["diff"]


@pytest.mark.asyncio
async def test_backend_host_processes_image_turn(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))

    client = CapturingApiClient("image received")
    host = ReactBackendHost(BackendHostConfig(api_client=client))
    host._bundle = await build_runtime(api_client=client)
    events = []

    async def _emit(event):
        events.append(event)

    host._emit = _emit  # type: ignore[method-assign]
    await start_runtime(host._bundle)
    try:
        should_continue = await host._process_line(
            "",
            images=[
                FrontendImageAttachment(
                    media_type="image/png",
                    data="aGVsbG8=",
                    source_path="clipboard:clipboard.png",
                )
            ],
        )
    finally:
        await close_runtime(host._bundle)

    assert should_continue is True
    assert len(client.requests) == 1
    image_messages = [
        message
        for message in client.requests[0].messages
        if message.role == "user" and any(isinstance(block, ImageBlock) for block in message.content)
    ]
    assert len(image_messages) == 1
    message = image_messages[0]
    assert message.text == "Please analyze the attached image."
    image = next(block for block in message.content if isinstance(block, ImageBlock))
    assert image.data == "aGVsbG8="
    assert any(
        event.type == "transcript_item"
        and event.item
        and event.item.role == "user"
        and event.item.text == "[1 image attached]"
        for event in events
    )


@pytest.mark.asyncio
async def test_backend_host_emits_compact_progress_event(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))

    host = ReactBackendHost(BackendHostConfig(api_client=StaticApiClient("unused")))
    host._bundle = await build_runtime(api_client=StaticApiClient("unused"))
    events = []

    async def _emit(event):
        events.append(event)

    async def _fake_handle_line(
        bundle,
        line,
        print_system,
        render_event,
        clear_output,
        user_message=None,
        print_user=None,
    ):
        del bundle, line, print_system, clear_output, user_message, print_user
        await render_event(
            CompactProgressEvent(
                phase="compact_start",
                trigger="auto",
                message="Compacting conversation memory.",
                checkpoint="compact_start",
                metadata={"token_count": 12345},
            )
        )
        return True

    monkeypatch.setattr("openharness.ui.backend_host.handle_line", _fake_handle_line)
    host._emit = _emit  # type: ignore[method-assign]
    await start_runtime(host._bundle)
    try:
        should_continue = await host._process_line("hi")
    finally:
        await close_runtime(host._bundle)

    assert should_continue is True
    assert any(
        event.type == "compact_progress"
        and event.compact_phase == "compact_start"
        and event.compact_checkpoint == "compact_start"
        and event.compact_metadata == {"token_count": 12345}
        for event in events
    )


@pytest.mark.asyncio
async def test_backend_host_surfaces_query_errors(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))

    host = ReactBackendHost(BackendHostConfig(api_client=FailingApiClient("rate limit")))
    host._bundle = await build_runtime(api_client=FailingApiClient("rate limit"))
    events = []

    async def _emit(event):
        events.append(event)

    host._emit = _emit  # type: ignore[method-assign]
    await start_runtime(host._bundle)
    try:
        should_continue = await host._process_line("hi")
    finally:
        await close_runtime(host._bundle)

    assert should_continue is True
    assert any(event.type == "error" and "rate limit" in event.message for event in events)
    assert any(
        event.type == "transcript_item"
        and event.item
        and event.item.role == "system"
        and "rate limit" in event.item.text
        for event in events
    )


@pytest.mark.asyncio
async def test_backend_host_command_does_not_reset_cli_overrides(tmp_path, monkeypatch):
    """Regression: slash commands should not snap model/provider back to persisted defaults.

    When the session is launched with CLI overrides (e.g. --provider openai -m 5.4),
    issuing a command like /fast triggers a UI state refresh. That refresh must
    preserve the effective session settings, not reload ~/.openharness/settings.json
    verbatim.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))

    host = ReactBackendHost(BackendHostConfig(api_client=StaticApiClient("unused")))
    host._bundle = await build_runtime(
        api_client=StaticApiClient("unused"),
        model="5.4",
        api_format="openai",
    )
    events = []

    async def _emit(event):
        events.append(event)

    host._emit = _emit  # type: ignore[method-assign]
    await start_runtime(host._bundle)
    try:
        # Sanity: the initial session state reflects CLI overrides.
        assert host._bundle.app_state.get().model == "5.4"
        assert host._bundle.app_state.get().provider == "openai-compatible"

        # Run a command that triggers sync_app_state.
        await host._process_line("/fast show")

        # CLI overrides should remain in effect.
        assert host._bundle.app_state.get().model == "5.4"
        assert host._bundle.app_state.get().provider == "openai-compatible"
    finally:
        await close_runtime(host._bundle)


@pytest.mark.asyncio
async def test_backend_host_uses_effective_model_from_env_override(tmp_path, monkeypatch):
    """Regression: header model should reflect effective env override, not stale profile last_model."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OPENHARNESS_MODEL", "minimax-m1")

    host = ReactBackendHost(BackendHostConfig(api_client=StaticApiClient("unused")))
    host._bundle = await build_runtime(api_client=StaticApiClient("unused"))
    events = []

    async def _emit(event):
        events.append(event)

    host._emit = _emit  # type: ignore[method-assign]
    await start_runtime(host._bundle)
    try:
        assert host._bundle.app_state.get().model == "minimax-m1"

        # Exercise sync_app_state through a slash command refresh path.
        await host._process_line("/fast show")
        assert host._bundle.app_state.get().model == "minimax-m1"
    finally:
        await close_runtime(host._bundle)


@pytest.mark.asyncio
async def test_build_runtime_leaves_interactive_sessions_unbounded_by_default(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))

    bundle = await build_runtime(
        api_client=StaticApiClient("unused"),
        enforce_max_turns=False,
    )
    try:
        assert bundle.engine.max_turns is None
        assert bundle.enforce_max_turns is False
    finally:
        await close_runtime(bundle)


@pytest.mark.asyncio
async def test_backend_host_emits_utf8_protocol_bytes(monkeypatch):
    host = ReactBackendHost(BackendHostConfig())
    fake_stdout = FakeBinaryStdout()
    monkeypatch.setattr("openharness.ui.backend_host.sys.stdout", fake_stdout)

    await host._emit(BackendEvent(type="assistant_delta", message="你好😊"))

    raw = fake_stdout.buffer.getvalue()
    assert raw.startswith(b"OHJSON:")
    decoded = raw.decode("utf-8").strip()
    payload = json.loads(decoded.removeprefix("OHJSON:"))
    assert payload["type"] == "assistant_delta"
    assert payload["message"] == "你好😊"


@pytest.mark.asyncio
async def test_backend_host_emits_model_select_request(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))

    host = ReactBackendHost(BackendHostConfig(api_client=StaticApiClient("unused")))
    host._bundle = await build_runtime(api_client=StaticApiClient("unused"), model="opus", api_format="anthropic")
    events = []

    async def _emit(event):
        events.append(event)

    host._emit = _emit  # type: ignore[method-assign]
    await start_runtime(host._bundle)
    try:
        await host._handle_select_command("model")
    finally:
        await close_runtime(host._bundle)

    event = next(item for item in events if item.type == "select_request")
    assert event.modal["command"] == "model"
    assert any(option["value"] == "opus" and option.get("active") for option in event.select_options)
    assert any(option["value"] == "default" for option in event.select_options)


@pytest.mark.asyncio
async def test_backend_host_model_selector_uses_profile_model_allowlist(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    save_settings(
        Settings().model_copy(
            update={
                "active_profile": "local-llm",
                "provider": "openai",
                "api_format": "openai",
                "base_url": "http://localhost:8000/v1",
                "model": "qwen-vl",
                "profiles": {
                    "local-llm": {
                        "label": "Local LLM",
                        "provider": "openai",
                        "api_format": "openai",
                        "auth_source": "openai_api_key",
                        "default_model": "deepseek-chat",
                        "last_model": "qwen-vl",
                        "base_url": "http://localhost:8000/v1",
                        "allowed_models": ["deepseek-chat", "qwen-vl"],
                    }
                },
            }
        )
    )

    host = ReactBackendHost(BackendHostConfig(api_client=StaticApiClient("unused")))
    host._bundle = await build_runtime(api_client=StaticApiClient("unused"))
    events = []

    async def _emit(event):
        events.append(event)

    host._emit = _emit  # type: ignore[method-assign]
    await start_runtime(host._bundle)
    try:
        await host._handle_select_command("model")
    finally:
        await close_runtime(host._bundle)

    event = next(item for item in events if item.type == "select_request")
    assert [option["value"] for option in event.select_options] == ["deepseek-chat", "qwen-vl"]
    assert any(option["value"] == "qwen-vl" and option.get("active") for option in event.select_options)


@pytest.mark.asyncio
async def test_backend_host_emits_theme_select_request(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))

    host = ReactBackendHost(BackendHostConfig(api_client=StaticApiClient("unused")))
    host._bundle = await build_runtime(api_client=StaticApiClient("unused"))
    events = []

    async def _emit(event):
        events.append(event)

    host._emit = _emit  # type: ignore[method-assign]
    await start_runtime(host._bundle)
    try:
        await host._handle_select_command("theme")
    finally:
        await close_runtime(host._bundle)

    event = next(item for item in events if item.type == "select_request")
    assert event.modal["command"] == "theme"
    assert any(option["value"] == "default" for option in event.select_options)


@pytest.mark.asyncio
async def test_backend_host_emits_turns_select_request_with_unlimited_option(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))

    host = ReactBackendHost(BackendHostConfig(api_client=StaticApiClient("unused")))
    host._bundle = await build_runtime(api_client=StaticApiClient("unused"), enforce_max_turns=False)
    events = []

    async def _emit(event):
        events.append(event)

    host._emit = _emit  # type: ignore[method-assign]
    await start_runtime(host._bundle)
    try:
        await host._handle_select_command("turns")
    finally:
        await close_runtime(host._bundle)

    event = next(item for item in events if item.type == "select_request")
    assert event.modal["command"] == "turns"
    assert any(option["value"] == "unlimited" and option.get("active") for option in event.select_options)


@pytest.mark.asyncio
async def test_backend_host_emits_provider_select_request(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))

    host = ReactBackendHost(BackendHostConfig(api_client=StaticApiClient("unused")))
    host._bundle = await build_runtime(api_client=StaticApiClient("unused"))
    events = []

    async def _emit(event):
        events.append(event)

    host._emit = _emit  # type: ignore[method-assign]
    await start_runtime(host._bundle)
    try:
        await host._handle_select_command("provider")
    finally:
        await close_runtime(host._bundle)

    event = next(item for item in events if item.type == "select_request")
    assert event.modal["command"] == "provider"
    assert any(option["value"] == "claude-api" and option.get("active") for option in event.select_options)


@pytest.mark.asyncio
async def test_backend_host_apply_select_command_shows_single_segment_transcript(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))

    host = ReactBackendHost(BackendHostConfig(api_client=StaticApiClient("unused")))
    host._bundle = await build_runtime(api_client=StaticApiClient("unused"))
    events = []

    async def _emit(event):
        events.append(event)

    host._emit = _emit  # type: ignore[method-assign]
    await start_runtime(host._bundle)
    try:
        should_continue = await host._apply_select_command("theme", "default")
    finally:
        await close_runtime(host._bundle)

    assert should_continue is True
    user_event = next(item for item in events if item.type == "transcript_item" and item.item and item.item.role == "user")
    assert user_event.item.text == "/theme"


@pytest.mark.asyncio
async def test_backend_host_apply_provider_select_command_shows_single_segment_transcript(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))

    host = ReactBackendHost(BackendHostConfig(api_client=StaticApiClient("unused")))
    host._bundle = await build_runtime(api_client=StaticApiClient("unused"))
    events = []

    async def _emit(event):
        events.append(event)

    host._emit = _emit  # type: ignore[method-assign]
    await start_runtime(host._bundle)
    try:
        should_continue = await host._apply_select_command("provider", "claude-api")
    finally:
        await close_runtime(host._bundle)

    assert should_continue is True
    user_event = next(item for item in events if item.type == "transcript_item" and item.item and item.item.role == "user")
    assert user_event.item.text == "/provider"


@pytest.mark.asyncio
async def test_concurrent_ask_permission_are_serialised():
    """Concurrent _ask_permission calls must be serialised so the frontend
    never receives two overlapping modal_request events.

    Without _permission_lock the second call emits a modal_request before the
    first future is resolved, overwriting the frontend's modal state. The first
    tool then silently waits 300 s and gets Permission denied.
    """
    host = ReactBackendHost(BackendHostConfig(api_client=StaticApiClient("unused")))

    emitted_order: list[str] = []

    async def _fake_emit(event: BackendEvent) -> None:
        if event.type == "modal_request" and event.modal:
            emitted_order.append(str(event.modal.get("request_id", "")))

    host._emit = _fake_emit  # type: ignore[method-assign]

    async def _ask_and_approve(tool: str) -> bool:
        # Start the ask; a background task resolves the future once it appears.
        async def _resolver():
            # Busy-wait until this tool's future is registered.
            while True:
                await asyncio.sleep(0)
                for rid, fut in list(host._permission_requests.items()):
                    if not fut.done():
                        fut.set_result(True)
                        return

        asyncio.create_task(_resolver())
        return await host._ask_permission(tool, "reason")

    # Fire two permission requests concurrently.
    result_a, result_b = await asyncio.gather(
        _ask_and_approve("write_file"),
        _ask_and_approve("bash"),
    )

    assert result_a is True
    assert result_b is True
    # With the lock in place the two modal_request events must be emitted
    # sequentially (one completes before the other starts), so exactly two
    # distinct request IDs must have been emitted.
    assert len(emitted_order) == 2
    assert emitted_order[0] != emitted_order[1]


@pytest.mark.asyncio
async def test_ask_edit_approval_remembers_always_choice():
    host = ReactBackendHost(BackendHostConfig(api_client=StaticApiClient("unused")))
    events = []

    async def _emit(event):
        events.append(event)

    host._emit = _emit  # type: ignore[method-assign]

    async def _resolver():
        while not host._edit_approval_requests:
            await asyncio.sleep(0)
        next(iter(host._edit_approval_requests.values())).set_result("always")

    asyncio.create_task(_resolver())
    reply = await host._ask_edit_approval("notes.txt", "@@ -1 +1 @@\n-old\n+new", 1, 1)

    assert reply == "always"
    assert host._edit_always_approved is True
    assert any(
        event.type == "modal_request"
        and event.modal
        and event.modal.get("kind") == "edit_diff"
        for event in events
    )

    events.clear()
    second_reply = await host._ask_edit_approval("notes.txt", "@@ -1 +1 @@\n-old\n+new", 1, 1)

    assert second_reply == "always"
    assert events == []


@pytest.mark.asyncio
async def test_ask_edit_approval_skips_when_session_mode_is_full_auto():
    host = ReactBackendHost(BackendHostConfig(api_client=StaticApiClient("unused")))
    events = []

    async def _emit(event):
        events.append(event)

    host._emit = _emit  # type: ignore[method-assign]
    host._bundle = SimpleNamespace(
        app_state=SimpleNamespace(get=lambda: SimpleNamespace(permission_mode="full_auto"))
    )

    reply = await host._ask_edit_approval("notes.txt", "@@ -1 +1 @@\n-old\n+new", 1, 1)

    assert reply == "always"
    assert events == []


class _PlanSwitchApiClient:
    """First turn asks for plan mode, second turn answers."""

    def __init__(self) -> None:
        self.calls = 0

    async def stream_message(self, request):
        del request
        self.calls += 1
        if self.calls == 1:
            yield ApiMessageCompleteEvent(
                message=ConversationMessage(
                    role="assistant",
                    content=[
                        ToolUseBlock(id="toolu_plan", name="enter_plan_mode", input={})
                    ],
                ),
                usage=UsageSnapshot(input_tokens=1, output_tokens=1),
                stop_reason="tool_use",
            )
            return
        yield ApiMessageCompleteEvent(
            message=ConversationMessage(
                role="assistant",
                content=[TextBlock(text="planning")],
            ),
            usage=UsageSnapshot(input_tokens=1, output_tokens=1),
            stop_reason="end_turn",
        )


@pytest.mark.asyncio
async def test_backend_host_emits_plan_mode_change_right_after_switch_tool(
    tmp_path, monkeypatch
):
    """The TUI must learn about a model-initiated switch before the turn ends."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))

    host = ReactBackendHost(
        BackendHostConfig(api_client=_PlanSwitchApiClient(), permission_mode="full_auto")
    )
    host._bundle = await build_runtime(
        api_client=host._config.api_client,
        permission_mode="full_auto",
    )
    events = []

    async def _emit(event):
        events.append(event)

    host._emit = _emit  # type: ignore[method-assign]
    await start_runtime(host._bundle)
    try:
        await host._process_line("make a plan")
    finally:
        await close_runtime(host._bundle)

    mode_index = next(
        index
        for index, event in enumerate(events)
        if event.type == "plan_mode_change" and event.plan_mode == "plan"
    )
    line_index = next(
        index for index, event in enumerate(events) if event.type == "line_complete"
    )
    assert mode_index < line_index
    assert host._bundle.engine.permission_mode.value == "plan"


@pytest.mark.asyncio
async def test_build_runtime_restores_work_state_without_stale_permission_mode(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))

    bundle = await build_runtime(
        api_client=StaticApiClient("unused"),
        restore_messages=[
            {"role": "user", "content": [{"type": "text", "text": "earlier turn"}]},
        ],
        restore_tool_metadata={
            "permission_mode": "plan",
            "read_file_state": [{"path": "src/app.py"}],
            "recent_work_log": ["edited src/app.py"],
            "unknown_key": "ignored",
        },
    )
    try:
        assert bundle.engine.tool_metadata["read_file_state"] == [{"path": "src/app.py"}]
        assert bundle.engine.tool_metadata["recent_work_log"] == ["edited src/app.py"]
        assert "unknown_key" not in bundle.engine.tool_metadata
        assert bundle.engine.tool_metadata["permission_mode"] != "plan"
        assert (
            bundle.engine.tool_metadata["permission_mode"]
            == bundle.app_state.get().permission_mode
        )
        assert [message.role for message in bundle.engine.messages] == ["user"]
    finally:
        await close_runtime(bundle)


@pytest.mark.asyncio
async def test_backend_host_replays_restored_messages_as_transcript():
    host = ReactBackendHost(
        BackendHostConfig(
            api_client=StaticApiClient("unused"),
            restore_messages=[
                {"role": "user", "content": [{"type": "text", "text": "earlier question"}]},
                {"role": "assistant", "content": [{"type": "text", "text": "earlier answer"}]},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "tool-1", "name": "bash", "input": {"command": "ls"}}
                    ],
                },
                {"role": "tool", "content": [{"type": "text", "text": "ignored"}]},
            ],
        )
    )
    events = []

    async def _emit(event):
        events.append(event)

    host._emit = _emit  # type: ignore[method-assign]

    await host._replay_restored_transcript()

    assert [(event.item.role, event.item.text) for event in events] == [
        ("user", "earlier question"),
        ("assistant", "earlier answer"),
    ]


@pytest.mark.asyncio
async def test_backend_host_reads_absolute_plan_file_for_todo_updates(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))

    plan_file = tmp_path / "sessions" / "plan-abc.md"
    plan_file.parent.mkdir(parents=True)
    plan_file.write_text("# Plan\n- [x] inspect\n- [ ] implement\n", encoding="utf-8")

    host = ReactBackendHost(BackendHostConfig(api_client=StaticApiClient("unused")))
    host._bundle = await build_runtime(api_client=StaticApiClient("unused"))
    events = []

    async def _emit(event):
        events.append(event)

    host._emit = _emit  # type: ignore[method-assign]
    await start_runtime(host._bundle)
    try:
        await host._emit_todo_file_update(str(plan_file))
    finally:
        await close_runtime(host._bundle)

    update = next(event for event in events if event.type == "todo_update")
    assert "- [x] inspect" in (update.todo_markdown or "")
    assert "- [ ] implement" in (update.todo_markdown or "")


@pytest.mark.asyncio
async def test_backend_host_repaints_todo_panel_when_plan_file_changes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))

    plan_dir = tmp_path / "sessions"
    plan_dir.mkdir(parents=True)
    plan_a = plan_dir / "plan-a.md"
    plan_a.write_text("# Plan\n- [ ] first\n", encoding="utf-8")
    plan_b = plan_dir / "plan-b.md"
    plan_b.write_text("# Plan\n- [x] second\n", encoding="utf-8")

    host = ReactBackendHost(BackendHostConfig(api_client=StaticApiClient("unused")))
    host._bundle = await build_runtime(api_client=StaticApiClient("unused"), cwd=str(tmp_path))
    updates: list[str] = []

    async def _emit(event):
        if event.type == "todo_update":
            updates.append(event.todo_markdown or "")

    host._emit = _emit  # type: ignore[method-assign]
    await start_runtime(host._bundle)
    try:
        host._last_plan_file = str(plan_a)
        host._bundle.engine.tool_metadata["plan_file"] = str(plan_b)
        await host._refresh_plan_file_todo()
        # An unchanged plan file must not repaint the panel.
        await host._refresh_plan_file_todo()
        # A resumed session without a checklist clears the panel instead of
        # leaving the previous session's items on screen.
        host._bundle.engine.tool_metadata["plan_file"] = str(plan_dir / "plan-gone.md")
        await host._refresh_plan_file_todo()
    finally:
        await close_runtime(host._bundle)

    assert updates == ["- [x] second", ""]


@pytest.mark.asyncio
async def test_todos_clear_repaints_the_panel_from_the_rewritten_plan_file(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))

    plan_file = tmp_path / "plan-sess.md"
    plan_file.write_text("# Plan\n- [x] first\n- [ ] second\n", encoding="utf-8")

    host = ReactBackendHost(
        BackendHostConfig(api_client=StaticApiClient("unused"), cwd=str(tmp_path))
    )
    host._bundle = await build_runtime(api_client=StaticApiClient("unused"), cwd=str(tmp_path))
    updates: list[str] = []

    async def _emit(event):
        if event.type == "todo_update":
            updates.append(event.todo_markdown or "")

    host._emit = _emit  # type: ignore[method-assign]
    await start_runtime(host._bundle)
    try:
        host._bundle.engine.tool_metadata["plan_file"] = str(plan_file)
        # Prime the panel with the checklist that is currently on disk.
        await host._refresh_plan_file_todo()
        # An unchanged file must not repaint.
        await host._refresh_plan_file_todo()
        # ``/todos clear`` keeps the path and rewrites the content; the panel
        # must follow, otherwise the deleted items stay on screen.
        await host._process_line("/todos clear")
    finally:
        await close_runtime(host._bundle)

    assert updates == ["- [x] first\n- [ ] second", ""]
    assert "- [" not in plan_file.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_resume_command_repaints_the_todo_panel(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))

    from openharness.services.session_storage import (
        get_project_session_dir,
        save_session_snapshot,
    )

    plan_file = get_project_session_dir(tmp_path) / "plan-restored1.md"
    plan_file.write_text("# Plan\n- [x] restored item\n", encoding="utf-8")
    save_session_snapshot(
        cwd=tmp_path,
        model="claude-test",
        system_prompt="system",
        messages=[ConversationMessage(role="user", content=[TextBlock(text="earlier")])],
        usage=UsageSnapshot(),
        session_id="restored1",
        tool_metadata={"plan_file": str(plan_file)},
    )

    host = ReactBackendHost(
        BackendHostConfig(api_client=StaticApiClient("unused"), cwd=str(tmp_path))
    )
    host._bundle = await build_runtime(api_client=StaticApiClient("unused"), cwd=str(tmp_path))
    updates: list[str] = []

    async def _emit(event):
        if event.type == "todo_update":
            updates.append(event.todo_markdown or "")

    host._emit = _emit  # type: ignore[method-assign]
    await start_runtime(host._bundle)
    try:
        await host._process_line("/resume restored1")
    finally:
        await close_runtime(host._bundle)

    assert "- [x] restored item" in updates[-1]


@pytest.mark.asyncio
async def test_agents_follow_streams_task_log_into_transcript(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))

    host = ReactBackendHost(BackendHostConfig(api_client=StaticApiClient("unused")))
    host._bundle = await build_runtime(api_client=StaticApiClient("unused"))
    events: list[BackendEvent] = []

    async def _emit(event):
        events.append(event)

    host._emit = _emit  # type: ignore[method-assign]
    await start_runtime(host._bundle)

    command = (
        f'"{sys.executable}" -u -c "import sys, time; '
        f"sys.stdin.readline(); print('hello follow', flush=True); time.sleep(0.3)\""
    )
    task = await get_task_manager().create_agent_task(
        prompt="go",
        description="follow smoke",
        cwd=tmp_path,
        command=command,
    )

    def _saw_line() -> bool:
        return any(
            event.type == "transcript_item"
            and event.item is not None
            and "hello follow" in event.item.text
            for event in events
        )

    try:
        await host._process_line(f"/agents follow {task.id}")
        deadline = asyncio.get_running_loop().time() + 5.0
        while not _saw_line():
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError("follow did not stream the task log")
            await asyncio.sleep(0.05)
    finally:
        await host._process_line("/agents unfollow all")
        await close_runtime(host._bundle)

    assert any(
        event.type == "transcript_item"
        and event.item is not None
        and f"Following {task.id}" in event.item.text
        for event in events
    )


@pytest.mark.asyncio
async def test_backend_host_lists_sessions_for_a_searchable_picker(tmp_path, monkeypatch):
    from openharness.services.session_backend import DEFAULT_SESSION_BACKEND
    from openharness.services.session_storage import save_session_snapshot

    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    for session_id in ("sid-1", "sid-2"):
        save_session_snapshot(
            cwd=tmp_path,
            model="opus",
            system_prompt="system",
            messages=[ConversationMessage.from_user_text(f"question {session_id}")],
            usage=UsageSnapshot(),
            session_id=session_id,
        )

    host = ReactBackendHost(BackendHostConfig())
    events = []

    async def _emit(event):
        events.append(event)

    host._emit = _emit  # type: ignore[method-assign]
    host._bundle = SimpleNamespace(  # type: ignore[assignment]
        cwd=str(tmp_path),
        session_id="sid-1",
        session_backend=DEFAULT_SESSION_BACKEND,
    )
    await host._handle_list_sessions()

    event = next(item for item in events if item.type == "select_request")
    assert event.modal["command"] == "resume"
    assert event.modal["searchable"] is True
    assert event.modal["title"].startswith("Resume Session")
    assert {option["value"] for option in event.select_options} == {"sid-1", "sid-2"}
    active = [option["value"] for option in event.select_options if option.get("active")]
    assert active == ["sid-1"]
    assert all(option["description"] == "opus" for option in event.select_options)


@pytest.mark.asyncio
async def test_backend_host_session_picker_asks_for_a_deep_list(tmp_path):
    seen: dict[str, object] = {}

    class _FakeBackend:
        def list_snapshots(self, cwd, limit=20):
            seen["limit"] = limit
            return []

    host = ReactBackendHost(BackendHostConfig())
    events = []

    async def _emit(event):
        events.append(event)

    host._emit = _emit  # type: ignore[method-assign]
    host._bundle = SimpleNamespace(  # type: ignore[assignment]
        cwd=str(tmp_path),
        session_id="",
        session_backend=_FakeBackend(),
    )
    await host._handle_list_sessions()

    assert seen["limit"] == 50
    event = next(item for item in events if item.type == "select_request")
    assert event.select_options == []


def test_only_read_only_commands_are_busy_safe():
    from openharness.commands.registry import create_default_command_registry

    registry = create_default_command_registry()
    busy_safe = {command.name for command in registry.list_commands() if command.busy_safe}

    assert {"status", "cost", "context", "diff", "help", "export"} <= busy_safe
    assert not {
        "clear",
        "compact",
        "resume",
        "model",
        "permissions",
        "tools",
        "mcp",
        "init",
        "review",
    } & busy_safe


@pytest.mark.asyncio
async def test_busy_safe_line_runs_without_clearing_the_turn_state(tmp_path, monkeypatch):
    from openharness.commands.registry import create_default_command_registry

    host = ReactBackendHost(BackendHostConfig(cwd=str(tmp_path)))
    events: list = []

    async def _emit(event):
        events.append(event)

    host._emit = _emit  # type: ignore[method-assign]
    host._bundle = SimpleNamespace(  # type: ignore[assignment]
        cwd=str(tmp_path),
        commands=create_default_command_registry(),
    )
    host._busy = True

    assert host._is_busy_safe_line("/status") is True
    assert host._is_busy_safe_line("/clear") is False
    assert host._is_busy_safe_line("hello there") is False

    await host._run_busy_safe_line("/status")

    assert host._busy is True
    assert not any(event.type == "line_complete" for event in events)
    assert any(event.type == "transcript_item" for event in events)


@pytest.mark.asyncio
async def test_backend_queues_prompts_typed_during_a_turn(tmp_path):
    host = ReactBackendHost(BackendHostConfig(cwd=str(tmp_path)))
    events: list = []
    queued: list = []

    async def _emit(event):
        events.append(event)

    class _Engine:
        def queue_user_message(self, message):
            queued.append(message)
            return True

    host._emit = _emit  # type: ignore[method-assign]
    host._bundle = SimpleNamespace(  # type: ignore[assignment]
        cwd=str(tmp_path),
        engine=_Engine(),
    )

    await host._queue_message_during_turn("also check the failing test")

    assert [message.text for message in queued] == ["also check the failing test"]
    user_items = [
        event for event in events
        if event.type == "transcript_item" and event.item.role == "user"
    ]
    assert [item.item.text for item in user_items] == ["also check the failing test"]
    assert any(
        event.type == "transcript_item" and "next tool boundary" in event.item.text
        for event in events
    )


@pytest.mark.asyncio
async def test_backend_reports_busy_when_the_engine_cannot_queue(tmp_path):
    host = ReactBackendHost(BackendHostConfig(cwd=str(tmp_path)))
    events: list = []

    async def _emit(event):
        events.append(event)

    class _Engine:
        def queue_user_message(self, message):
            del message
            return False

    host._emit = _emit  # type: ignore[method-assign]
    host._bundle = SimpleNamespace(cwd=str(tmp_path), engine=_Engine())  # type: ignore[assignment]

    await host._queue_message_during_turn("hello")

    assert [event.type for event in events] == ["error"]


@pytest.mark.asyncio
async def test_background_request_without_a_running_command_is_a_no_op(tmp_path):
    host = ReactBackendHost(BackendHostConfig(cwd=str(tmp_path)))
    events: list = []

    async def _emit(event):
        events.append(event)

    class _Engine:
        def request_background(self):
            return False

    host._emit = _emit  # type: ignore[method-assign]
    host._bundle = SimpleNamespace(cwd=str(tmp_path), engine=_Engine())  # type: ignore[assignment]

    await host._background_active_tool()

    assert any("Nothing to background" in event.item.text for event in events)


@pytest.mark.asyncio
async def test_set_ui_preference_persists_the_todo_panel(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    from openharness.config.settings import load_settings
    from openharness.state import AppState, AppStateStore

    host = ReactBackendHost(BackendHostConfig(cwd=str(tmp_path)))
    events: list = []

    async def _emit(event):
        events.append(event)

    host._emit = _emit  # type: ignore[method-assign]
    app_state = AppStateStore(
        AppState(model="test", permission_mode="default", theme="default")
    )
    host._bundle = SimpleNamespace(  # type: ignore[assignment]
        cwd=str(tmp_path),
        engine=SimpleNamespace(),
        app_state=app_state,
    )
    host._status_snapshot = lambda: BackendEvent.state_snapshot(app_state.get())  # type: ignore[method-assign]

    await host._set_ui_preference("todo_panel", "compact")

    assert load_settings().todo_panel == "compact"
    assert host._bundle.app_state.get().todo_panel == "compact"
    snapshot = next(event for event in events if event.type == "state_snapshot")
    assert snapshot.state["todo_panel"] == "compact"


@pytest.mark.asyncio
async def test_set_ui_preference_rejects_unknown_keys_and_values(tmp_path):
    host = ReactBackendHost(BackendHostConfig(cwd=str(tmp_path)))
    events: list = []

    async def _emit(event):
        events.append(event)

    host._emit = _emit  # type: ignore[method-assign]
    host._bundle = SimpleNamespace(  # type: ignore[assignment]
        cwd=str(tmp_path),
        engine=SimpleNamespace(),
        app_state=None,
    )

    await host._set_ui_preference("todo_panel", "sideways")
    await host._set_ui_preference("nonsense", "full")

    assert [event.type for event in events] == ["error", "error"]


@pytest.mark.asyncio
async def test_turns_run_as_their_own_task_so_the_loop_stays_responsive(tmp_path):
    host = ReactBackendHost(BackendHostConfig(cwd=str(tmp_path)))
    release = asyncio.Event()
    finished = asyncio.Event()

    async def _process_line(line, *, transcript_line=None, images=None, end_of_turn=True, echo=True):
        del line, transcript_line, images, end_of_turn, echo
        await release.wait()
        finished.set()
        return True

    host._process_line = _process_line  # type: ignore[method-assign]
    host._busy = True
    host._start_turn("hello")

    # The caller gets control back immediately: the turn is its own task.
    await asyncio.sleep(0)
    assert finished.is_set() is False
    assert host._busy is True
    turn = host._active_request_task
    assert turn is not None

    release.set()
    await asyncio.wait_for(turn, timeout=2)
    assert finished.is_set() is True
    assert host._busy is False


@pytest.mark.asyncio
async def test_a_turn_that_exits_wakes_the_request_loop(tmp_path):
    host = ReactBackendHost(BackendHostConfig(cwd=str(tmp_path)))

    async def _process_line(line, *, transcript_line=None, images=None, end_of_turn=True, echo=True):
        del line, transcript_line, images, end_of_turn, echo
        return False

    host._process_line = _process_line  # type: ignore[method-assign]
    host._busy = True
    host._start_turn("/exit")

    request = await asyncio.wait_for(host._request_queue.get(), timeout=2)

    assert request.type == "shutdown"


@pytest.mark.asyncio
async def test_shell_commands_run_as_their_own_task(tmp_path):
    """`!cmd` must not freeze the request loop either."""
    host = ReactBackendHost(BackendHostConfig(cwd=str(tmp_path)))
    release = asyncio.Event()
    finished = asyncio.Event()

    async def _run_shell_command(line: str) -> None:
        del line
        await release.wait()
        finished.set()

    host._run_shell_command = _run_shell_command  # type: ignore[method-assign]
    host._busy = True
    host._start_detached(_run_shell_command("sleep 10"))
    for _ in range(12):
        await asyncio.sleep(0)

    assert finished.is_set() is False
    assert host._busy is True
    release.set()
    await asyncio.wait_for(host._active_request_task, timeout=2)
    assert finished.is_set() is True
    assert host._busy is False


@pytest.mark.asyncio
async def test_leftover_followups_get_their_own_turn(tmp_path):
    host = ReactBackendHost(BackendHostConfig(cwd=str(tmp_path)))
    pending = [ConversationMessage.from_user_text("late follow-up")]
    started: list[tuple[str, bool]] = []

    class _Engine:
        def take_pending_user_messages(self):
            messages = list(pending)
            pending.clear()
            return messages

    host._bundle = SimpleNamespace(cwd=str(tmp_path), engine=_Engine())  # type: ignore[assignment]
    host._start_turn = lambda line, images=None, *, echo=True: started.append((line, echo))  # type: ignore[method-assign]

    assert host._start_pending_followup_turn() is True
    assert started == [("late follow-up", False)]
    assert host._busy is True
    assert host._start_pending_followup_turn() is False
