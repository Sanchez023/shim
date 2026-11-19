"""Local prompt-prefix fingerprints and cache-miss attribution.

Providers with implicit prefix caching (DeepSeek, OpenAI) only report cached
token counts; they do not tell us *why* a prefix missed.  This module keeps a
per-session fingerprint of the bytes we sent and classifies each request as a
cold start, a system/tool drift, a history rewrite, or a normal append.  The
classification is deliberately local: it never needs the provider tokenizer and
never affects the request itself.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, cast

from openharness.domain.messages import (
    ConversationMessage,
    ImageBlock,
    ReasoningBlock,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from openharness.domain.usage import UsageSnapshot

log = logging.getLogger(__name__)

# A hit-rate drop below this floor is only interesting when the absolute miss
# is large enough to matter (and is not explained by a cold start/compaction).
_ALERT_HIT_RATE = 0.70
_ALERT_MIN_MISS_TOKENS = 2_000
_ALERT_MIN_MISS_FRACTION = 0.05


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _is_prefix(previous: tuple[str, ...], current: tuple[str, ...]) -> bool:
    if len(previous) > len(current):
        return False
    return current[: len(previous)] == previous


def _first_changed_section(
    previous: tuple[tuple[str, str], ...],
    current: tuple[tuple[str, str], ...],
) -> str | None:
    """Return the first prompt section whose hash changed, if any."""
    previous_map = dict(previous)
    for name, digest in current:
        if previous_map.get(name) != digest:
            return name
    current_names = {name for name, _ in current}
    for name, _ in previous:
        if name not in current_names:
            return name
    return None


@dataclass(frozen=True)
class CacheFingerprint:
    """Byte-level identity of the stable part of one model request."""

    model: str
    system_sha: str
    #: Hash of the contiguous cacheable prefix only.  Tail-only changes (plan
    #: mode, /fast, /effort, relevant memory) do not invalidate the provider
    #: prefix and must not be reported as system drift.
    stable_sha: str
    tools_sha: str
    message_hashes: tuple[str, ...]
    section_hashes: tuple[tuple[str, str], ...] = ()

    @property
    def label(self) -> str:
        """Return a compact fingerprint suitable for logs."""
        return f"{self.system_sha}:{self.tools_sha}:{len(self.message_hashes)}"


def _block_wire_payload(block: Any) -> dict[str, Any]:
    """Return only the fields a provider actually serializes for a block."""
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, ImageBlock):
        return {
            "type": "image",
            "media_type": block.media_type,
            "data": block.data,
        }
    if isinstance(block, ToolUseBlock):
        return {
            "type": "tool_use",
            "id": block.id,
            "name": block.name,
            "input": block.input,
        }
    if isinstance(block, ToolResultBlock):
        return {
            "type": "tool_result",
            "tool_use_id": block.tool_use_id,
            "content": block.content,
            "is_error": block.is_error,
        }
    if isinstance(block, ThinkingBlock):
        return {
            "type": "thinking",
            "thinking": block.thinking,
            "signature": block.signature,
            "redacted": block.redacted,
            "data": block.data,
        }
    if isinstance(block, ReasoningBlock):
        return {
            "type": "reasoning",
            "provider": block.provider,
            "text": block.text,
            "item_id": block.item_id,
            "summary": block.summary,
            "encrypted_content": block.encrypted_content,
            "raw": block.raw,
        }
    return cast(dict[str, Any], block.model_dump(mode="json"))


def _message_wire_payload(message: ConversationMessage) -> str:
    """Serialize the provider-visible shape, excluding local-only metadata.

    ``result_metadata``, ``provider_item_id``, ``visibility`` and
    ``source_path`` do not change the bytes sent to the model.  Hashing them
    would report false ``history_rewrite`` events when the harness updates its
    local bookkeeping.
    """
    payload = {
        "role": message.role,
        "content": [_block_wire_payload(block) for block in message.content],
    }
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    # OpenAI-compatible providers replay captured reasoning on tool-call
    # assistant messages; include it so a replay change is visible.
    reasoning = getattr(message, "_reasoning", "")
    if reasoning:
        text += reasoning
    return text


def fingerprint_request(
    *,
    model: str,
    system_prompt: str,
    tools: Iterable[dict[str, Any]],
    messages: Iterable[ConversationMessage],
    section_hashes: tuple[tuple[str, str], ...] = (),
    system_prompt_cacheable_prefix: str | None = None,
) -> CacheFingerprint:
    """Build a deterministic fingerprint for one outgoing request."""
    tools_json = json.dumps(
        list(tools),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    message_hashes = [
        _sha(_message_wire_payload(message)) for message in messages
    ]
    stable_text = (
        system_prompt
        if system_prompt_cacheable_prefix is None
        else system_prompt_cacheable_prefix
    )
    return CacheFingerprint(
        model=model,
        system_sha=_sha(system_prompt or ""),
        stable_sha=_sha(stable_text or ""),
        tools_sha=_sha(tools_json),
        message_hashes=tuple(message_hashes),
        section_hashes=tuple(section_hashes),
    )


@dataclass
class CacheDiagnostics:
    """Fold cache usage into session-level attribution counters."""

    requests: int = 0
    cold_starts: int = 0
    provider_cold_starts: int = 0
    system_drifts: int = 0
    tool_drifts: int = 0
    history_rewrites: int = 0
    anomaly_misses: int = 0
    alert_count: int = 0
    prompt_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    hot_prompt_tokens: int = 0
    hot_cache_hit_tokens: int = 0
    hot_cache_miss_tokens: int = 0
    _previous: CacheFingerprint | None = field(default=None, repr=False)

    def record(
        self,
        fingerprint: CacheFingerprint,
        usage: UsageSnapshot,
    ) -> dict[str, Any]:
        """Classify one request and return a JSON-safe diagnostic payload."""
        previous = self._previous
        classification: str
        first_changed_section: str | None = None
        cold_start = previous is None or previous.model != fingerprint.model

        if cold_start:
            classification = "cold_start"
            self.cold_starts += 1
        else:
            assert previous is not None
            if previous.stable_sha != fingerprint.stable_sha:
                classification = "system_drift"
                first_changed_section = _first_changed_section(
                    previous.section_hashes, fingerprint.section_hashes
                )
                self.system_drifts += 1
            elif previous.tools_sha != fingerprint.tools_sha:
                classification = "tools_drift"
                self.tool_drifts += 1
            elif not _is_prefix(previous.message_hashes, fingerprint.message_hashes):
                classification = "history_rewrite"
                self.history_rewrites += 1
            elif len(previous.message_hashes) == len(fingerprint.message_hashes):
                classification = "unchanged"
            else:
                classification = "history_append"
            if (
                first_changed_section is None
                and previous.system_sha != fingerprint.system_sha
            ):
                # Tail-only change: report the section for diagnostics, but do
                # not count it as prefix drift.
                first_changed_section = _first_changed_section(
                    previous.section_hashes, fingerprint.section_hashes
                )

        self.requests += 1
        prompt_tokens = usage.prompt_tokens
        hit_tokens = usage.cache_hit_tokens
        miss_tokens = usage.cache_miss_tokens
        hit_rate = usage.cache_hit_rate

        self.prompt_tokens += prompt_tokens
        self.cache_hit_tokens += hit_tokens
        self.cache_miss_tokens += miss_tokens
        # A session-first request can still be warm if startup warmup or an
        # earlier session populated the provider's prefix cache.  Only exclude
        # requests that actually observed a full miss from the hot rate.
        provider_cold = cold_start and prompt_tokens > 0 and hit_tokens == 0
        if provider_cold:
            self.provider_cold_starts += 1
        if not provider_cold:
            self.hot_prompt_tokens += prompt_tokens
            self.hot_cache_hit_tokens += hit_tokens
            self.hot_cache_miss_tokens += miss_tokens

        if (
            not cold_start
            and hit_rate is not None
            and hit_rate <= 0.0
            and prompt_tokens > 0
        ):
            self.anomaly_misses += 1

        alert = self._should_alert(classification, prompt_tokens, miss_tokens, hit_rate)
        if alert:
            self.alert_count += 1

        self._previous = fingerprint
        payload = {
            "classification": classification,
            "cold_start": cold_start,
            "prefix_fingerprint": fingerprint.label,
            "prompt_tokens": prompt_tokens,
            "cache_hit_tokens": hit_tokens,
            "cache_miss_tokens": miss_tokens,
            "cache_hit_rate": hit_rate,
            "first_changed_section": first_changed_section,
            "tail_changed": (
                not cold_start
                and previous is not None
                and previous.system_sha != fingerprint.system_sha
                and previous.stable_sha == fingerprint.stable_sha
            ),
            "alert": alert,
        }
        if alert:
            payload["alert_message"] = (
                f"Prompt cache hit rate dropped to {hit_rate:.0%} "
                f"({miss_tokens} miss tokens, {classification})."
            )
            log.warning(
                "Prompt cache hit rate drop: %.1f%% miss=%d classification=%s section=%s",
                (hit_rate or 0.0) * 100,
                miss_tokens,
                classification,
                first_changed_section,
            )
        return payload

    @staticmethod
    def _should_alert(
        classification: str,
        prompt_tokens: int,
        miss_tokens: int,
        hit_rate: float | None,
    ) -> bool:
        """Return True for fixable drift, not expected cold starts/compaction."""
        if hit_rate is None or hit_rate >= _ALERT_HIT_RATE:
            return False
        if classification in {"cold_start", "history_rewrite"}:
            return False
        threshold = max(_ALERT_MIN_MISS_TOKENS, int(prompt_tokens * _ALERT_MIN_MISS_FRACTION))
        return miss_tokens >= threshold

    @property
    def last_fingerprint(self) -> CacheFingerprint | None:
        """Return the most recent request fingerprint, if any.

        Exposed so cache-stability gates and the per-PR cache delta can read
        ``stable_sha`` / ``tools_sha`` without reaching into ``_previous``.
        """
        return self._previous

    def summary(self) -> dict[str, Any]:
        """Return session-level counters for ``/usage`` and the status bar."""
        last = self._previous
        return {
            "requests": self.requests,
            "stable_sha": last.stable_sha if last is not None else None,
            "tools_sha": last.tools_sha if last is not None else None,
            "cold_starts": self.cold_starts,
            "provider_cold_starts": self.provider_cold_starts,
            "system_drifts": self.system_drifts,
            "tool_drifts": self.tool_drifts,
            "history_rewrites": self.history_rewrites,
            "anomaly_misses": self.anomaly_misses,
            "alerts": self.alert_count,
            "prompt_tokens": self.prompt_tokens,
            "cache_hit_tokens": self.cache_hit_tokens,
            "cache_miss_tokens": self.cache_miss_tokens,
            "cache_hit_rate": (
                self.cache_hit_tokens / self.prompt_tokens
                if self.prompt_tokens > 0
                else None
            ),
            "hot_requests": max(0, self.requests - self.provider_cold_starts),
            "hot_cache_hit_rate": (
                self.hot_cache_hit_tokens / self.hot_prompt_tokens
                if self.hot_prompt_tokens > 0
                else None
            ),
        }

    def reset(self) -> None:
        self.requests = 0
        self.cold_starts = 0
        self.provider_cold_starts = 0
        self.system_drifts = 0
        self.tool_drifts = 0
        self.history_rewrites = 0
        self.anomaly_misses = 0
        self.alert_count = 0
        self.prompt_tokens = 0
        self.cache_hit_tokens = 0
        self.cache_miss_tokens = 0
        self.hot_prompt_tokens = 0
        self.hot_cache_hit_tokens = 0
        self.hot_cache_miss_tokens = 0
        self._previous = None
