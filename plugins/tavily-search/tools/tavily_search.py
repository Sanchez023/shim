"""Tavily web search tool contributed by the tavily-search plugin."""

from __future__ import annotations

from typing import Literal

import httpx
from pydantic import BaseModel, Field

from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult

TAVILY_SEARCH_URL = "https://api.tavily.com/search"
_MAX_RESULT_SNIPPET = 400
_MAX_OUTPUT_CHARS = 8_000

_API_KEY_ENV_VARS = ("TAVILY_API_KEY", "TVLY_API_KEY", "OPENHARNESS_TAVILY_API_KEY")


class TavilySearchInput(BaseModel):
    """Arguments for a Tavily web search."""

    query: str = Field(description="Search query")
    max_results: int = Field(default=5, ge=1, le=20, description="Maximum number of results")
    search_depth: Literal["basic", "advanced"] = Field(
        default="basic",
        description="Tavily search depth; 'advanced' costs more credits but returns richer snippets",
    )
    topic: Literal["general", "news", "finance"] = Field(
        default="general",
        description="Search topic",
    )
    include_answer: bool = Field(
        default=False,
        description="Ask Tavily for a synthesized answer in addition to the result list",
    )
    include_domains: list[str] = Field(
        default_factory=list,
        description="Only return results from these domains",
    )
    exclude_domains: list[str] = Field(
        default_factory=list,
        description="Never return results from these domains",
    )
    days: int | None = Field(
        default=None,
        ge=1,
        le=365,
        description="For topic='news', only return results from the last N days",
    )
    time_range: Literal["day", "week", "month", "year"] | None = Field(
        default=None,
        description="Restrict results to a relative time range",
    )


def _resolve_api_key() -> str | None:
    from openharness.plugins.config import get_plugin_variable

    return get_plugin_variable(
        "tavily-search",
        "TAVILY_API_KEY",
        env_names=_API_KEY_ENV_VARS,
    )


def _build_payload(arguments: TavilySearchInput) -> dict[str, object]:
    payload: dict[str, object] = {
        "query": arguments.query,
        "max_results": arguments.max_results,
        "search_depth": arguments.search_depth,
        "topic": arguments.topic,
        "include_answer": arguments.include_answer,
    }
    if arguments.include_domains:
        payload["include_domains"] = arguments.include_domains
    if arguments.exclude_domains:
        payload["exclude_domains"] = arguments.exclude_domains
    if arguments.days is not None:
        payload["days"] = arguments.days
    if arguments.time_range is not None:
        payload["time_range"] = arguments.time_range
    return payload


def format_tavily_results(query: str, payload: dict[str, object]) -> str:
    """Render a Tavily response payload into compact, model-friendly text."""
    lines = [f"Tavily results for: {query}"]

    answer = payload.get("answer")
    if isinstance(answer, str) and answer.strip():
        lines.extend(["", f"Answer: {answer.strip()}"])

    results = payload.get("results")
    if not isinstance(results, list) or not results:
        return "\n".join([*lines, "", "No search results found."])

    for index, item in enumerate(results, start=1):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip() or "(untitled)"
        url = str(item.get("url") or "").strip()
        content = " ".join(str(item.get("content") or "").split())
        published = str(item.get("published_date") or "").strip()
        score = item.get("score")

        lines.extend(["", f"{index}. {title}"])
        if url:
            lines.append(f"   URL: {url}")
        if published:
            lines.append(f"   Published: {published}")
        if isinstance(score, (int, float)):
            lines.append(f"   Score: {float(score):.3f}")
        if content:
            lines.append(f"   {content[:_MAX_RESULT_SNIPPET]}")

    output = "\n".join(lines)
    if len(output) > _MAX_OUTPUT_CHARS:
        output = output[:_MAX_OUTPUT_CHARS] + "\n... (truncated)"
    return output


class TavilySearchTool(BaseTool):
    """Search the web with Tavily and return ranked results."""

    name = "tavily_search"
    description = (
        "Search the web with Tavily and return ranked results with titles, URLs, "
        "snippets, scores, and an optional synthesized answer. Requires a Tavily "
        "API key in TAVILY_API_KEY."
    )
    input_model = TavilySearchInput

    def is_read_only(self, arguments: TavilySearchInput) -> bool:
        del arguments
        return True

    async def execute(
        self,
        arguments: TavilySearchInput,
        context: ToolExecutionContext,
    ) -> ToolResult:
        del context
        api_key = _resolve_api_key()
        if api_key is None:
            return ToolResult(
                output=(
                    "tavily_search requires a Tavily API key. Run "
                    "`oh plugin config set tavily-search TAVILY_API_KEY` or set the "
                    "TAVILY_API_KEY environment variable. Get a key at "
                    "https://app.tavily.com/."
                ),
                is_error=True,
            )

        try:
            async with httpx.AsyncClient(
                timeout=30.0,
                follow_redirects=False,
                trust_env=True,
            ) as client:
                response = await client.post(
                    TAVILY_SEARCH_URL,
                    json=_build_payload(arguments),
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                        "User-Agent": "OpenHarness/tavily-search",
                    },
                )
        except httpx.HTTPError as exc:
            return ToolResult(output=f"tavily_search request failed: {exc}", is_error=True)

        if response.status_code == 401:
            return ToolResult(
                output="tavily_search failed: Tavily rejected the API key (401). Check TAVILY_API_KEY.",
                is_error=True,
            )
        if response.status_code in {429, 432}:
            return ToolResult(
                output=(
                    "tavily_search failed: Tavily rate limit or plan limit reached "
                    f"({response.status_code}). Retry later or check the Tavily plan."
                ),
                is_error=True,
            )
        if response.status_code >= 400:
            detail = response.text.strip().replace("\n", " ")[:300]
            return ToolResult(
                output=f"tavily_search failed: HTTP {response.status_code}: {detail}",
                is_error=True,
            )

        try:
            payload = response.json()
        except ValueError as exc:
            return ToolResult(output=f"tavily_search returned invalid JSON: {exc}", is_error=True)
        if not isinstance(payload, dict):
            return ToolResult(output="tavily_search returned an unexpected payload.", is_error=True)

        return ToolResult(output=format_tavily_results(arguments.query, payload))
