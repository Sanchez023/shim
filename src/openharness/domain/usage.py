"""Usage tracking models (layer-0, ADR-0010 D2)."""

from __future__ import annotations

from pydantic import BaseModel


class UsageSnapshot(BaseModel):
    """Token usage returned by the model provider.

    ``input_tokens`` counts uncached prompt tokens only, matching Anthropic's
    accounting. OpenAI-style providers report cached tokens inside their prompt
    total; adapters split that number out into ``cache_read_input_tokens`` so
    every provider sums the same way.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    @property
    def prompt_tokens(self) -> int:
        """Return all prompt tokens, including cache reads and writes."""
        return (
            self.input_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )

    @property
    def total_tokens(self) -> int:
        """Return the total number of accounted tokens."""
        return self.prompt_tokens + self.output_tokens

    @property
    def cache_hit_tokens(self) -> int:
        """Return prompt tokens served from a provider cache.

        Providers with implicit prefix caching (DeepSeek, OpenAI) report these
        as "cached" prompt tokens; Anthropic reports them as cache reads.  The
        adapter normalizes both into ``cache_read_input_tokens``.
        """
        return self.cache_read_input_tokens

    @property
    def cache_miss_tokens(self) -> int:
        """Return prompt tokens that were not served from cache."""
        return self.input_tokens + self.cache_creation_input_tokens

    @property
    def cache_hit_rate(self) -> float | None:
        """Return the fraction of prompt tokens that hit the cache.

        ``None`` means the provider did not report prompt usage at all, which
        is different from a genuine 0% hit rate.
        """
        prompt = self.prompt_tokens
        if prompt <= 0:
            return None
        return self.cache_hit_tokens / prompt
