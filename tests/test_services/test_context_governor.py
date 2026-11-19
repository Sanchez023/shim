from __future__ import annotations

import pytest

from openharness.api.usage import UsageSnapshot
from openharness.engine.messages import ConversationMessage, TextBlock
from openharness.runtime.seams.governance.context import (
    ExistingCompactGovernor,
    NoCompactGovernor,
)


@pytest.mark.asyncio
async def test_existing_governor_defers_to_builtin_compaction():
    governor = ExistingCompactGovernor()

    plan = await governor.on_step_pre(
        [ConversationMessage.from_user_text("hi")],
        model="test",
        context_window_tokens=1000,
        auto_compact_threshold_tokens=800,
    )

    assert plan is None


@pytest.mark.asyncio
async def test_no_compact_governor_disables_compaction():
    governor = NoCompactGovernor()

    plan = await governor.on_step_pre(
        [ConversationMessage.from_user_text("hi")],
        model="test",
    )

    assert plan is not None
    assert plan.skip_compaction is True


@pytest.mark.asyncio
async def test_governor_can_supply_replacement_messages():
    class _ReplacingGovernor:
        async def on_step_pre(self, messages, **kwargs):
            del messages, kwargs
            from openharness.runtime.seams.governance.context import ContextPlan

            return ContextPlan(
                messages=[
                    ConversationMessage(
                        role="assistant", content=[TextBlock(text="summary")]
                    )
                ],
                reason="test",
            )

        async def on_request_error(self, messages, error):
            del messages, error

        async def after_turn(self, messages, usage):
            del messages, usage

    governor = _ReplacingGovernor()
    plan = await governor.on_step_pre(
        [ConversationMessage.from_user_text("hi")],
        model="test",
    )

    assert plan is not None
    assert plan.messages is not None
    assert plan.messages[0].text == "summary"


@pytest.mark.asyncio
async def test_no_compact_governor_after_turn_is_noop():
    governor = NoCompactGovernor()

    await governor.after_turn([ConversationMessage.from_user_text("hi")], UsageSnapshot())
