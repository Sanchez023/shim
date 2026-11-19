"""Tests for local prompt-cache miss attribution."""

from __future__ import annotations

import pytest

from openharness.api.usage import UsageSnapshot
from openharness.engine.cache_diagnostics import (
    CacheDiagnostics,
    fingerprint_request,
)
from openharness.engine.messages import ConversationMessage


def _fp(
    *,
    model: str = "deepseek-chat",
    system: str = "SYSTEM",
    tools: list[dict] | None = None,
    messages: list[ConversationMessage] | None = None,
    sections: tuple[tuple[str, str], ...] = (("base", "aaa"),),
):
    return fingerprint_request(
        model=model,
        system_prompt=system,
        tools=tools or [{"name": "read_file"}],
        messages=messages or [ConversationMessage.from_user_text("hello")],
        section_hashes=sections,
    )


def _usage(hit: int, miss: int, output: int = 10) -> UsageSnapshot:
    return UsageSnapshot(
        input_tokens=miss,
        output_tokens=output,
        cache_read_input_tokens=hit,
    )


def test_first_request_is_a_cold_start_and_does_not_alert():
    diagnostics = CacheDiagnostics()

    result = diagnostics.record(_fp(), _usage(hit=0, miss=5_800))

    assert result["classification"] == "cold_start"
    assert result["cold_start"] is True
    assert result["alert"] is False
    assert diagnostics.summary()["cold_starts"] == 1
    assert diagnostics.summary()["hot_cache_hit_rate"] is None


def test_warm_first_request_counts_as_hot_for_the_provider():
    diagnostics = CacheDiagnostics()

    result = diagnostics.record(_fp(), _usage(hit=5_632, miss=128))

    assert result["classification"] == "cold_start"
    summary = diagnostics.summary()
    assert summary["cold_starts"] == 1
    assert summary["provider_cold_starts"] == 0
    assert summary["hot_requests"] == 1
    assert summary["hot_cache_hit_rate"] == pytest.approx(5_632 / 5_760)


def test_history_append_reports_hot_hit_rate():
    diagnostics = CacheDiagnostics()
    messages = [ConversationMessage.from_user_text("hello")]
    diagnostics.record(_fp(messages=messages), _usage(hit=0, miss=1_000))
    messages = [*messages, ConversationMessage.from_user_text("next")]

    result = diagnostics.record(
        _fp(messages=messages), _usage(hit=900, miss=100)
    )

    assert result["classification"] == "history_append"
    summary = diagnostics.summary()
    assert summary["cache_hit_rate"] == 900 / 2_000
    assert summary["hot_cache_hit_rate"] == 0.9


def test_system_drift_reports_the_first_changed_section():
    diagnostics = CacheDiagnostics()
    diagnostics.record(_fp(system="SYSTEM A"), _usage(hit=0, miss=1_000))

    result = diagnostics.record(
        _fp(
            system="SYSTEM B",
            sections=(("base", "bbb"), ("personality", "ccc")),
        ),
        _usage(hit=500, miss=500),
    )

    assert result["classification"] == "system_drift"
    assert result["first_changed_section"] == "base"
    assert diagnostics.summary()["system_drifts"] == 1


def test_tail_only_change_is_not_reported_as_system_drift():
    diagnostics = CacheDiagnostics()

    def _fingerprint(system: str, permission_hash: str):
        return fingerprint_request(
            model="deepseek-chat",
            system_prompt=system,
            system_prompt_cacheable_prefix="STABLE PREFIX",
            tools=[{"name": "read_file"}],
            messages=[ConversationMessage.from_user_text("hello")],
            section_hashes=(
                ("base", "stable"),
                ("permission_mode", permission_hash),
            ),
        )

    diagnostics.record(
        _fingerprint("STABLE PREFIX\n\nPLAN", "plan"), _usage(hit=0, miss=1_000)
    )
    result = diagnostics.record(
        _fingerprint("STABLE PREFIX\n\nAUTO", "auto"), _usage(hit=900, miss=100)
    )

    assert result["classification"] == "unchanged"
    assert result["tail_changed"] is True
    assert result["first_changed_section"] == "permission_mode"
    assert diagnostics.summary()["system_drifts"] == 0


def test_tool_drift_is_tracked_separately():
    diagnostics = CacheDiagnostics()
    diagnostics.record(_fp(), _usage(hit=0, miss=1_000))

    result = diagnostics.record(
        _fp(tools=[{"name": "read_file"}, {"name": "tool_search"}]),
        _usage(hit=500, miss=500),
    )

    assert result["classification"] == "tools_drift"
    assert diagnostics.summary()["tool_drifts"] == 1


def test_history_rewrite_is_not_treated_as_a_fixable_drift():
    diagnostics = CacheDiagnostics()
    messages = [
        ConversationMessage.from_user_text("one"),
        ConversationMessage.from_user_text("two"),
    ]
    diagnostics.record(_fp(messages=messages), _usage(hit=0, miss=5_800))

    rewritten = [ConversationMessage.from_user_text("[summary]")]
    result = diagnostics.record(
        _fp(messages=rewritten), _usage(hit=0, miss=2_800)
    )

    assert result["classification"] == "history_rewrite"
    assert result["alert"] is False
    assert diagnostics.summary()["history_rewrites"] == 1


def test_large_fixable_drift_triggers_an_alert():
    diagnostics = CacheDiagnostics()
    diagnostics.record(_fp(system="SYSTEM A"), _usage(hit=0, miss=5_800))

    result = diagnostics.record(
        _fp(system="SYSTEM B", sections=(("base", "bbb"),)),
        _usage(hit=1_000, miss=4_800),
    )

    assert result["classification"] == "system_drift"
    assert result["alert"] is True
    assert "hit rate dropped" in result["alert_message"]
    assert diagnostics.summary()["alerts"] == 1


def test_fingerprint_ignores_non_wire_result_metadata():
    from openharness.engine.messages import ToolResultBlock

    first = ConversationMessage(
        role="user",
        content=[
            ToolResultBlock(
                tool_use_id="tool-1",
                content="same output",
                result_metadata={"artifact": "a"},
            )
        ],
    )
    second = ConversationMessage(
        role="user",
        content=[
            ToolResultBlock(
                tool_use_id="tool-1",
                content="same output",
                result_metadata={"artifact": "b"},
            )
        ],
    )
    changed = ConversationMessage(
        role="user",
        content=[ToolResultBlock(tool_use_id="tool-1", content="different")],
    )

    base = fingerprint_request(
        model="deepseek-chat",
        system_prompt="SYSTEM",
        tools=[],
        messages=[first],
    )
    same_wire = fingerprint_request(
        model="deepseek-chat",
        system_prompt="SYSTEM",
        tools=[],
        messages=[second],
    )
    different = fingerprint_request(
        model="deepseek-chat",
        system_prompt="SYSTEM",
        tools=[],
        messages=[changed],
    )

    assert base.message_hashes == same_wire.message_hashes
    assert base.message_hashes != different.message_hashes
