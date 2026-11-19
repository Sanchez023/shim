"""Usage accounting across providers."""

from __future__ import annotations

import pytest

from openharness.api.codex_client import _usage_from_response
from openharness.api.openai_client import _usage_data_from_chunk
from openharness.api.usage import UsageSnapshot
from openharness.engine.cost_tracker import CostTracker


def test_usage_snapshot_counts_cache_tokens_in_prompt_and_total():
    usage = UsageSnapshot(
        input_tokens=10,
        output_tokens=5,
        cache_creation_input_tokens=100,
        cache_read_input_tokens=200,
    )

    assert usage.prompt_tokens == 310
    assert usage.total_tokens == 315


def test_cost_tracker_accumulates_cache_fields():
    tracker = CostTracker()
    tracker.add(
        UsageSnapshot(
            input_tokens=1,
            output_tokens=2,
            cache_creation_input_tokens=3,
            cache_read_input_tokens=4,
        )
    )
    tracker.add(
        UsageSnapshot(
            input_tokens=10,
            output_tokens=20,
            cache_read_input_tokens=40,
        )
    )

    assert tracker.total == UsageSnapshot(
        input_tokens=11,
        output_tokens=22,
        cache_creation_input_tokens=3,
        cache_read_input_tokens=44,
    )


def test_usage_snapshot_exposes_hit_and_miss_counters():
    usage = UsageSnapshot(
        input_tokens=400,
        output_tokens=5,
        cache_creation_input_tokens=100,
        cache_read_input_tokens=500,
    )

    assert usage.cache_hit_tokens == 500
    assert usage.cache_miss_tokens == 500
    assert usage.cache_hit_rate == pytest.approx(0.5)


def test_usage_snapshot_hit_rate_is_none_without_prompt_usage():
    assert UsageSnapshot().cache_hit_rate is None


def test_deepseek_usage_reads_prompt_cache_fields() -> None:
    class _Usage:
        prompt_tokens = 5760
        completion_tokens = 40
        prompt_cache_hit_tokens = 5632
        prompt_cache_miss_tokens = 128

        def model_dump(self):
            return {
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "prompt_cache_hit_tokens": self.prompt_cache_hit_tokens,
                "prompt_cache_miss_tokens": self.prompt_cache_miss_tokens,
            }

    assert _usage_data_from_chunk(_Usage()) == {
        "input_tokens": 128,
        "output_tokens": 40,
        "cache_read_input_tokens": 5632,
    }


def test_deepseek_usage_derives_hit_from_miss_only() -> None:
    class _Usage:
        prompt_tokens = 128
        completion_tokens = 4
        prompt_cache_miss_tokens = 64

    assert _usage_data_from_chunk(_Usage()) == {
        "input_tokens": 64,
        "output_tokens": 4,
        "cache_read_input_tokens": 64,
    }


def test_openai_usage_splits_cached_tokens_out_of_prompt() -> None:
    class _Details:
        cached_tokens = 300

    class _Usage:
        prompt_tokens = 1000
        completion_tokens = 50
        prompt_tokens_details = _Details()

    assert _usage_data_from_chunk(_Usage()) == {
        "input_tokens": 700,
        "output_tokens": 50,
        "cache_read_input_tokens": 300,
    }


def test_openai_usage_without_details_reports_no_cache() -> None:
    class _Usage:
        prompt_tokens = 10
        completion_tokens = 2
        prompt_tokens_details = None

    assert _usage_data_from_chunk(_Usage()) == {
        "input_tokens": 10,
        "output_tokens": 2,
        "cache_read_input_tokens": 0,
    }


def test_codex_usage_reads_cached_input_details() -> None:
    usage = _usage_from_response(
        {
            "usage": {
                "input_tokens": 1000,
                "output_tokens": 20,
                "input_tokens_details": {"cached_tokens": 400},
            }
        }
    )

    assert usage.input_tokens == 600
    assert usage.cache_read_input_tokens == 400
    assert usage.output_tokens == 20
    assert usage.prompt_tokens == 1000
