"""Keep new tool-result tokens inside a cache-friendly budget.

Prompt caching is prefix based: on the request that first carries a tool
result, every token of that result is a cache miss.  The hit-rate ceiling for
that request is ``P / (P + N)``, where ``P`` is the prompt from the previous
request and ``N`` is the new content.  A 95% target therefore allows roughly
``P / 19`` new tokens.

This module shrinks the largest tool results before they are written to the
model-visible history.  The full output is preserved in the tool-artifact
directory, so the model can still read it on demand with ``read_file`` or
``grep``; only the inline preview is shortened.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable

from openharness.domain.messages import ToolResultBlock
from openharness.engine.tool_dispatch import offload_tool_output_if_needed
from openharness.utils.tokens import estimate_tokens

log = logging.getLogger(__name__)

DEFAULT_TARGET_HIT_RATE = 0.95
DEFAULT_CACHE_PREVIEW_CHARS = 1_200
MIN_CACHE_PREVIEW_CHARS = 400
_TRUNCATED_HEADER = "[Tool output truncated]"


def _read_float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("Ignoring invalid %s=%r", name, raw)
        return default


def _read_int_env(name: str, default: int, *, minimum: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        log.warning("Ignoring invalid %s=%r", name, raw)
        return default


def cache_budget_enabled() -> bool:
    raw = os.environ.get("OPENHARNESS_CACHE_BUDGET", "").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def cache_target_hit_rate() -> float:
    value = _read_float_env(
        "OPENHARNESS_CACHE_TARGET_HIT_RATE", DEFAULT_TARGET_HIT_RATE
    )
    # Keep the target in a range where the budget is meaningful.
    return min(max(value, 0.50), 0.995)


def cache_preview_chars() -> int:
    return _read_int_env(
        "OPENHARNESS_CACHE_PREVIEW_CHARS",
        DEFAULT_CACHE_PREVIEW_CHARS,
        minimum=MIN_CACHE_PREVIEW_CHARS,
    )


def new_token_budget(
    previous_prompt_tokens: int,
    target_hit_rate: float | None = None,
) -> int:
    """Return how many new tokens fit in the target hit-rate ceiling."""
    if previous_prompt_tokens <= 0:
        return 0
    target = cache_target_hit_rate() if target_hit_rate is None else target_hit_rate
    target = min(max(float(target), 0.50), 0.995)
    return max(0, int(previous_prompt_tokens * (1.0 - target) / target))


def _shrink_already_truncated(content: str, preview_chars: int) -> str:
    """Shorten the preview of an already-offloaded result without re-saving it."""
    header, separator, preview = content.partition("\n\nPreview:\n")
    if not separator:
        return content
    if len(preview) <= preview_chars:
        return content
    return (
        f"{header}{separator}{preview[:preview_chars].rstrip()}\n"
        "...[preview shortened to fit the cache budget]..."
    )


def _result_tokens(content: str) -> int:
    return estimate_tokens(content)


def shrink_tool_results_for_budget(
    tool_results: Iterable[ToolResultBlock],
    *,
    tool_names: dict[str, str],
    previous_prompt_tokens: int,
    target_hit_rate: float | None = None,
) -> int:
    """Shrink the largest tool results to fit ``previous_prompt_tokens``.

    Returns the estimated number of prompt tokens saved.  Mutates the
    ``ToolResultBlock`` objects in place so the model-visible message and the
    persisted history stay consistent.
    """
    if not cache_budget_enabled() or previous_prompt_tokens <= 0:
        return 0

    results = [result for result in tool_results if result.content]
    if not results:
        return 0

    budget = new_token_budget(previous_prompt_tokens, target_hit_rate)
    current = sum(_result_tokens(result.content) for result in results)
    if current <= budget:
        return 0

    preview_limit = cache_preview_chars()
    # Shrink the biggest results first; small results keep their context.
    ordered = sorted(results, key=lambda result: len(result.content), reverse=True)
    saved = 0
    for result in ordered:
        if current <= budget:
            break
        before = _result_tokens(result.content)
        if result.content.startswith(_TRUNCATED_HEADER):
            new_content = _shrink_already_truncated(result.content, preview_limit)
        else:
            new_content, artifact_path = offload_tool_output_if_needed(
                tool_name=tool_names.get(result.tool_use_id, "tool"),
                tool_use_id=result.tool_use_id,
                output=result.content,
                inline_limit=0,
                preview_chars=preview_limit,
            )
            if artifact_path is not None:
                log.info(
                    "Cache budget offloaded tool result %s to %s",
                    result.tool_use_id,
                    artifact_path,
                )
        after = _result_tokens(new_content)
        if after >= before:
            continue
        result.content = new_content
        current -= before - after
        saved += before - after

    if saved:
        log.info(
            "Cache budget shrank tool results by ~%d tokens (prompt=%d, budget=%d)",
            saved,
            previous_prompt_tokens,
            budget,
        )
    return saved
