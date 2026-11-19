"""Tests for the web_search backends: DuckDuckGo HTML and Bing RSS."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from openharness.tools.base import ToolExecutionContext
from openharness.tools.web_search_tool import (
    WebSearchTool,
    WebSearchToolInput,
    _is_bing_host,
    _parse_rss_results,
    _search_params,
)

BING_RSS = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel>
<title>必应：python asyncio</title>
<item>
  <title>Python Docs</title>
  <link>https://docs.python.org/3/library/asyncio.html</link>
  <description>Official asyncio documentation.</description>
</item>
<item>
  <title><![CDATA[Real Python]]></title>
  <link>https://realpython.com/async-io-python/</link>
  <description><![CDATA[<b>Async</b> IO &amp; coroutines.]]></description>
</item>
</channel></rss>"""


def _patch_fetch(monkeypatch, fake) -> None:
    monkeypatch.setitem(WebSearchTool.execute.__globals__, "fetch_public_http_response", fake)


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("https://cn.bing.com/search", "rss"),
        ("https://www.bing.com/search", "rss"),
        ("https://bing.com/search", "rss"),
        ("https://notbing.com/search", None),
        ("https://bing.com.example.org/search", None),
        ("https://cn.bing.com/search?format=json", None),
    ],
)
def test_search_params_requests_rss_only_for_bing_hosts(endpoint: str, expected: str | None) -> None:
    params = _search_params(endpoint, "python asyncio")
    assert params["q"] == "python asyncio"
    assert params.get("format") == expected


def test_is_bing_host_rejects_lookalike_domains() -> None:
    assert _is_bing_host("cn.bing.com")
    assert _is_bing_host("BING.COM")
    assert not _is_bing_host("notbing.com")
    assert not _is_bing_host("bing.com.evil.example")
    assert not _is_bing_host(None)


def test_parse_rss_results_cleans_cdata_and_respects_limit() -> None:
    results = _parse_rss_results(BING_RSS, limit=2)
    assert len(results) == 2
    assert results[0] == {
        "title": "Python Docs",
        "url": "https://docs.python.org/3/library/asyncio.html",
        "snippet": "Official asyncio documentation.",
    }
    assert results[1]["title"] == "Real Python"
    assert results[1]["snippet"] == "Async IO & coroutines."

    assert len(_parse_rss_results(BING_RSS, limit=1)) == 1


def test_parse_rss_results_skips_items_without_title_or_link() -> None:
    body = """<rss><channel>
    <item><title>No link</title></item>
    <item><link>https://example.com/no-title</link></item>
    <item><title>Good</title><link>https://example.com/good</link></item>
    </channel></rss>"""

    results = _parse_rss_results(body, limit=5)

    assert results == [{"title": "Good", "url": "https://example.com/good", "snippet": ""}]


@pytest.mark.asyncio
async def test_web_search_tool_parses_bing_rss(tmp_path, monkeypatch):
    seen: dict[str, object] = {}

    async def fake_fetch(url: str, **kwargs: object) -> httpx.Response:
        seen["url"] = url
        seen["params"] = kwargs.get("params")
        request = httpx.Request("GET", url, params=kwargs.get("params"))
        return httpx.Response(
            200,
            headers={"Content-Type": "text/xml; charset=utf-8"},
            text=BING_RSS,
            request=request,
        )

    _patch_fetch(monkeypatch, fake_fetch)

    result = await WebSearchTool().execute(
        WebSearchToolInput(
            query="python asyncio",
            max_results=2,
            search_url="https://cn.bing.com/search",
        ),
        ToolExecutionContext(cwd=tmp_path),
    )

    assert result.is_error is False
    assert seen["url"] == "https://cn.bing.com/search"
    assert seen["params"] == {"q": "python asyncio", "format": "rss"}
    assert "Real Python" in result.output
    assert "https://realpython.com/async-io-python/" in result.output
    assert "<b>" not in result.output


@pytest.mark.asyncio
async def test_web_search_tool_falls_back_to_settings_search_url(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENHARNESS_WEB_SEARCH_URL", raising=False)
    monkeypatch.setattr(
        "openharness.config.load_settings",
        lambda: SimpleNamespace(web=SimpleNamespace(search_url="https://cn.bing.com/search")),
    )
    seen: dict[str, object] = {}

    async def fake_fetch(url: str, **kwargs: object) -> httpx.Response:
        seen["url"] = url
        seen["params"] = kwargs.get("params")
        request = httpx.Request("GET", url, params=kwargs.get("params"))
        return httpx.Response(200, text=BING_RSS, request=request)

    _patch_fetch(monkeypatch, fake_fetch)

    result = await WebSearchTool().execute(
        WebSearchToolInput(query="python asyncio"),
        ToolExecutionContext(cwd=tmp_path),
    )

    assert result.is_error is False
    assert seen["url"] == "https://cn.bing.com/search"
    assert seen["params"] == {"q": "python asyncio", "format": "rss"}


@pytest.mark.asyncio
async def test_web_search_tool_reports_error_when_rss_is_empty(tmp_path, monkeypatch):
    async def fake_fetch(url: str, **kwargs: object) -> httpx.Response:
        request = httpx.Request("GET", url, params=kwargs.get("params"))
        return httpx.Response(
            200,
            headers={"Content-Type": "text/xml; charset=utf-8"},
            text="<rss version='2.0'><channel></channel></rss>",
            request=request,
        )

    _patch_fetch(monkeypatch, fake_fetch)

    result = await WebSearchTool().execute(
        WebSearchToolInput(
            query="nothing matches",
            search_url="https://cn.bing.com/search",
        ),
        ToolExecutionContext(cwd=tmp_path),
    )

    assert result.is_error is True
    assert result.output == "No search results found."


@pytest.mark.asyncio
async def test_web_search_truncates_long_snippets(tmp_path, monkeypatch):
    body = (
        "<rss><channel><item>"
        "<title>Long result</title>"
        "<link>https://example.com/long</link>"
        f"<description>{'x' * 1200}</description>"
        "</item></channel></rss>"
    )

    async def fake_fetch(url: str, **kwargs: object) -> httpx.Response:
        request = httpx.Request("GET", url, params=kwargs.get("params"))
        return httpx.Response(
            200,
            headers={"Content-Type": "text/xml; charset=utf-8"},
            text=body,
            request=request,
        )

    _patch_fetch(monkeypatch, fake_fetch)

    result = await WebSearchTool().execute(
        WebSearchToolInput(query="q", search_url="https://cn.bing.com/search"),
        ToolExecutionContext(cwd=tmp_path),
    )

    assert result.is_error is False
    snippet = next(line.strip() for line in result.output.splitlines() if line.strip().startswith("x"))
    assert len(snippet) == 401
    assert snippet.endswith("…")
    assert "x" * 401 not in result.output


@pytest.mark.asyncio
async def test_web_search_pairs_ddg_snippets_with_results(tmp_path, monkeypatch):
    body = """
    <html><body>
      <a href="https://example.com/nav">Navigation</a>
      <a class="result__a" href="https://example.com/one">First result</a>
      <a class="result__snippet" href="https://example.com/one">First snippet</a>
      <a class="result__a" href="https://example.com/two">Second result</a>
      <a class="result__snippet" href="https://example.com/two">Second snippet</a>
      <a href="https://example.com/footer">Footer</a>
      <a class="result__a" href="https://example.com/three">Third result</a>
      <a class="result__snippet" href="https://example.com/three">Third snippet</a>
    </body></html>
    """

    async def fake_fetch(url: str, **kwargs: object) -> httpx.Response:
        request = httpx.Request("GET", url, params=kwargs.get("params"))
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html; charset=utf-8"},
            text=body,
            request=request,
        )

    _patch_fetch(monkeypatch, fake_fetch)

    result = await WebSearchTool().execute(
        WebSearchToolInput(
            query="query",
            max_results=3,
            search_url="https://search.example.com/html",
        ),
        ToolExecutionContext(cwd=tmp_path),
    )

    assert result.is_error is False
    lines = result.output.splitlines()
    first = lines.index("1. First result")
    second = lines.index("2. Second result")
    third = lines.index("3. Third result")
    assert any("First snippet" in line for line in lines[first:second])
    assert any("Second snippet" in line for line in lines[second:third])
    assert any("Third snippet" in line for line in lines[third:])
