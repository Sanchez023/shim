from __future__ import annotations

from pathlib import Path

import pytest

from openharness.api.usage import UsageSnapshot
from openharness.engine.events import AgentEvent
from openharness.engine.messages import ConversationMessage, TextBlock
from openharness.services.event_log import InMemoryEventLogStore
from openharness.services.session_store import SessionStore


def _event(seq: int, kind: str, payload: dict) -> AgentEvent:
    return AgentEvent(
        event_id=f"e{seq}",
        session_id="s1",
        turn_id="t1",
        step_id="step1",
        seq=seq,
        ts=float(seq),
        kind=kind,
        payload=payload,
    )


@pytest.mark.asyncio
async def test_session_store_derives_messages_and_usage(tmp_path: Path):
    log = InMemoryEventLogStore()
    await log.append(
        _event(
            1,
            "user.message",
            {"message": ConversationMessage.from_user_text("hi").model_dump(mode="json")},
        )
    )
    await log.append(
        _event(
            2,
            "assistant.message",
            {
                "message": ConversationMessage(
                    role="assistant", content=[TextBlock(text="hello")]
                ).model_dump(mode="json"),
                "usage": {"input_tokens": 2, "output_tokens": 1},
            },
        )
    )
    store = SessionStore(cwd=tmp_path, event_log=log)

    messages = await store.derive_messages()
    usage = await store.derive_usage()
    snapshot = await store.snapshot()

    assert [message.text for message in messages] == ["hi", "hello"]
    assert usage == UsageSnapshot(input_tokens=2, output_tokens=1)
    assert snapshot["last_seq"] == 2
    assert len(snapshot["messages"]) == 2
