"""Simple web search tool."""

from __future__ import annotations

import html
import os
import re
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from pydantic import BaseModel, Field

from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult
from openharness.utils.network_guard import NetworkGuardError, fetch_public_http_response


class WebSearchToolInput(BaseModel):
    """Arguments for a web search."""

    query: str = Field(description="Search query")
    max_results: int = Field(default=5, ge=1, le=10, description="Maximum number of results")
    search_url: str | None = Field(
        default=None,
        description="Optional override for the HTML search endpoint, useful for private search backends or testing.",
    )


class WebSearchTool(BaseTool[WebSearchToolInput]):
    """Run a web search and return compact top results."""

    name = "web_search"
    description = "Search the web and return compact top results with titles, URLs, and snippets."
    input_model = WebSearchToolInput

    def is_read_only(self, arguments: WebSearchToolInput) -> bool:
        del arguments
        return True

    async def execute(
        self,
        arguments: WebSearchToolInput,
        context: ToolExecutionContext,
    ) -> ToolResult:
        del context
        endpoint = _resolve_search_endpoint(arguments.search_url)
        try:
            response = await fetch_public_http_response(
                endpoint,
                params=_search_params(endpoint, arguments.query),
                headers={"User-Agent": "OpenHarness/0.1"},
                timeout=20.0,
            )
            response.raise_for_status()
        except (httpx.HTTPError, NetworkGuardError) as exc:
            return ToolResult(output=f"web_search failed: {exc}", is_error=True)

        results = _parse_results(response, limit=arguments.max_results)
        if not results:
            return ToolResult(output="No search results found.", is_error=True)

        lines = [f"Search results for: {arguments.query}"]
        for index, result in enumerate(results, start=1):
            lines.append(f"{index}. {result['title']}")
            lines.append(f"   URL: {result['url']}")
            snippet = result["snippet"]
            if snippet:
                if len(snippet) > _MAX_SNIPPET_CHARS:
                    snippet = snippet[:_MAX_SNIPPET_CHARS].rstrip() + "…"
                lines.append(f"   {snippet}")
        return ToolResult(output="\n".join(lines))


DEFAULT_SEARCH_URL = "https://html.duckduckgo.com/html/"
_MAX_SNIPPET_CHARS = 400


def _resolve_search_endpoint(explicit: str | None) -> str:
    """Resolve the endpoint: explicit argument > env var > settings > default."""
    if explicit:
        return explicit
    from_env = os.environ.get("OPENHARNESS_WEB_SEARCH_URL")
    if from_env:
        return from_env
    from openharness.config import load_settings

    return load_settings().web.search_url or DEFAULT_SEARCH_URL


def _search_params(endpoint: str, query: str) -> dict[str, str]:
    """Build query params, asking Bing for its RSS feed.

    Bing's HTML result page carries none of the markers this parser looks for,
    so the RSS variant is requested explicitly instead.
    """
    params = {"q": query}
    parsed = urlparse(endpoint)
    if _is_bing_host(parsed.hostname) and "format=" not in (parsed.query or ""):
        params["format"] = "rss"
    return params


def _is_bing_host(hostname: str | None) -> bool:
    """Return True for ``bing.com`` and its subdomains (not lookalikes)."""
    host = (hostname or "").lower()
    return host == "bing.com" or host.endswith(".bing.com")


def _parse_results(response: httpx.Response, *, limit: int) -> list[dict[str, str]]:
    """Pick a parser that matches the response body."""
    if _looks_like_rss(response):
        return _parse_rss_results(response.text, limit=limit)
    return _parse_search_results(response.text, limit=limit)


def _looks_like_rss(response: httpx.Response) -> bool:
    content_type = response.headers.get("content-type", "").lower()
    if "xml" in content_type or "rss" in content_type:
        return True
    head = response.text[:512].lstrip()
    return head.startswith(("<?xml", "<rss", "<feed"))


def _parse_rss_results(body: str, *, limit: int) -> list[dict[str, str]]:
    """Parse RSS 2.0 ``<item>`` entries into result dicts."""
    results: list[dict[str, str]] = []
    for match in re.finditer(r"<item>(?P<item>.*?)</item>", body, flags=re.IGNORECASE | re.DOTALL):
        item = match.group("item")
        title = _clean_html(_first_tag(item, "title"))
        url = _clean_html(_first_tag(item, "link"))
        snippet = _clean_html(_first_tag(item, "description"))
        if title and url:
            results.append({"title": title, "url": url, "snippet": snippet})
        if len(results) >= limit:
            break
    return results


def _first_tag(fragment: str, tag: str) -> str:
    match = re.search(rf"<{tag}>(?P<value>.*?)</{tag}>", fragment, flags=re.IGNORECASE | re.DOTALL)
    if match is None:
        return ""
    value = match.group("value").strip()
    if value.startswith("<![CDATA[") and value.endswith("]]>"):
        return value[len("<![CDATA[") : -len("]]>")].strip()
    return value


_SEARCH_TOKEN_PATTERN = re.compile(
    r'<(?:a|div|span)[^>]+class="[^"]*(?:result__snippet|result-snippet)[^"]*"[^>]*>'
    r"(?P<snippet>.*?)</(?:a|div|span)>"
    r"|"
    r"<a(?P<attrs>[^>]+)>(?P<title>.*?)</a>",
    flags=re.IGNORECASE | re.DOTALL,
)


def _parse_search_results(body: str, *, limit: int) -> list[dict[str, str]]:
    """Parse DuckDuckGo HTML results in document order.

    Snippets belong to the result that precedes them in the page, so they are
    attached while scanning instead of being indexed by an anchor counter
    (which also counts logos, navigation links, and snippet anchors).
    """
    results: list[dict[str, str]] = []
    current: dict[str, str] | None = None

    for match in _SEARCH_TOKEN_PATTERN.finditer(body):
        if match.group("title") is None:
            snippet = _clean_html(match.group("snippet"))
            if current is not None and not current["snippet"] and snippet:
                current["snippet"] = snippet
            continue

        attrs = match.group("attrs")
        class_match = re.search(r'class="(?P<class>[^"]+)"', attrs, flags=re.IGNORECASE)
        if class_match is None:
            continue
        class_names = class_match.group("class")
        if "result__a" not in class_names and "result-link" not in class_names:
            continue
        href_match = re.search(r'href="(?P<href>[^"]+)"', attrs, flags=re.IGNORECASE)
        if href_match is None:
            continue
        title = _clean_html(match.group("title"))
        url = _normalize_result_url(href_match.group("href"))
        if not title or not url:
            continue
        if len(results) >= limit:
            current = None
            continue
        current = {"title": title, "url": url, "snippet": ""}
        results.append(current)

    return results


def _normalize_result_url(raw_url: str) -> str:
    parsed = urlparse(raw_url)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        return unquote(target) if target else raw_url
    return raw_url


def _clean_html(fragment: str) -> str:
    text = re.sub(r"(?s)<[^>]+>", " ", fragment)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text
