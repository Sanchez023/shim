"""Compaction tasks must not outlive an abandoned turn."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from openharness.engine.messages import ConversationMessage
from openharness.engine.stream_events import CompactProgressEvent
from openharness.engine.turn_executor import TurnExecutor
from openharness.services.compact_port import ServiceCompactPort


@pytest.mark.asyncio
async def test_stream_compaction_cancels_background_task_when_consumer_stops(monkeypatch):
    cancelled = asyncio.Event()

    async def fake_auto_compact(*args, **kwargs):
        progress = kwargs["progress_callback"]
        await progress(CompactProgressEvent(phase="compact_start", trigger="auto"))
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(
        "openharness.services.compact.auto_compact_if_needed",
        fake_auto_compact,
    )
    context = SimpleNamespace(
        api_client=None,
        model="test-model",
        system_prompt="system",
        hook_executor=None,
        tool_metadata={},
        context_window_tokens=None,
        auto_compact_threshold_tokens=None,
        event_sink=None,
        event_sequencer=None,
        turn_id="",
        current_step_id="",
        compact_port=ServiceCompactPort(),
    )
    executor = TurnExecutor(context)
    stream = executor._stream_compaction(
        [ConversationMessage.from_user_text("hi")],
        trigger="auto",
    )

    event, _ = await anext(stream)
    assert event.phase == "compact_start"

    await stream.aclose()
    await asyncio.wait_for(cancelled.wait(), timeout=1)
