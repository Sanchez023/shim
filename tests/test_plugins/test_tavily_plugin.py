"""Tests for the bundled tavily-search plugin."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import ClassVar

import pytest
from typing_extensions import Self  # noqa: UP035 - keeps tests runnable on Python 3.10

from openharness.config.settings import Settings
from openharness.plugins import load_plugins
from openharness.tools.base import ToolExecutionContext

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = REPO_ROOT / "plugins"
TOOL_PATH = PLUGIN_ROOT / "tavily-search" / "tools" / "tavily_search.py"


def _load_tool_module():
    module_name = "tavily_search_under_test"
    spec = importlib.util.spec_from_file_location(module_name, TOOL_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class _FakeResponse:
    def __init__(self, *, status_code: int = 200, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = "error body" if status_code >= 400 else ""

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    captured: ClassVar[dict[str, object]] = {}
    response = _FakeResponse()

    def __init__(self, *args, **kwargs) -> None:
        del args, kwargs

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info) -> bool:
        del exc_info
        return False

    async def post(self, url: str, *, json: dict, headers: dict) -> _FakeResponse:
        type(self).captured = {"url": url, "json": json, "headers": headers}
        return type(self).response


def _isolate_credentials(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr("openharness.auth.storage._keyring_checked", True)
    monkeypatch.setattr("openharness.auth.storage._keyring_usable", False)


def test_plugin_loads_its_tool(tmp_path: Path, monkeypatch) -> None:
    _isolate_credentials(monkeypatch, tmp_path)

    plugins = load_plugins(Settings(), tmp_path, extra_roots=[PLUGIN_ROOT])

    tavily = next(plugin for plugin in plugins if plugin.name == "tavily-search")
    assert tavily.enabled is True
    assert [tool.name for tool in tavily.tools] == ["tavily_search"]
    assert tavily.manifest.variables["TAVILY_API_KEY"].required is True
    assert tavily.manifest.variables["TAVILY_API_KEY"].secret is True


@pytest.mark.asyncio
async def test_missing_api_key_is_a_clear_error(monkeypatch, tmp_path: Path) -> None:
    module = _load_tool_module()
    _isolate_credentials(monkeypatch, tmp_path)
    for name in module._API_KEY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    tool = module.TavilySearchTool()

    result = await tool.execute(
        module.TavilySearchInput(query="openharness"),
        ToolExecutionContext(cwd=tmp_path),
    )

    assert result.is_error is True
    assert "TAVILY_API_KEY" in result.output


@pytest.mark.asyncio
async def test_search_returns_formatted_results(monkeypatch, tmp_path: Path) -> None:
    module = _load_tool_module()
    _isolate_credentials(monkeypatch, tmp_path)
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    monkeypatch.setattr(module.httpx, "AsyncClient", _FakeClient)
    _FakeClient.response = _FakeResponse(
        payload={
            "answer": "OpenHarness is an AI coding assistant.",
            "results": [
                {
                    "title": "OpenHarness",
                    "url": "https://example.com/openharness",
                    "content": "A Python port of Claude Code.\nWith tools.",
                    "score": 0.91,
                    "published_date": "2026-11-16",
                }
            ],
        }
    )
    tool = module.TavilySearchTool()

    result = await tool.execute(
        module.TavilySearchInput(
            query="openharness",
            max_results=3,
            search_depth="advanced",
            include_domains=["example.com"],
        ),
        ToolExecutionContext(cwd=tmp_path),
    )

    assert result.is_error is False
    assert "Answer: OpenHarness is an AI coding assistant." in result.output
    assert "1. OpenHarness" in result.output
    assert "https://example.com/openharness" in result.output
    assert "Score: 0.910" in result.output
    assert _FakeClient.captured["url"] == "https://api.tavily.com/search"
    assert _FakeClient.captured["headers"]["Authorization"] == "Bearer tvly-test"
    assert _FakeClient.captured["json"] == {
        "query": "openharness",
        "max_results": 3,
        "search_depth": "advanced",
        "topic": "general",
        "include_answer": False,
        "include_domains": ["example.com"],
    }


@pytest.mark.asyncio
async def test_stored_plugin_credential_is_used(monkeypatch, tmp_path: Path) -> None:
    module = _load_tool_module()
    _isolate_credentials(monkeypatch, tmp_path)
    for name in module._API_KEY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    from openharness.plugins.config import set_plugin_variable

    set_plugin_variable("tavily-search", "TAVILY_API_KEY", "tvly-stored")
    monkeypatch.setattr(module.httpx, "AsyncClient", _FakeClient)
    _FakeClient.response = _FakeResponse(payload={"results": []})

    result = await module.TavilySearchTool().execute(
        module.TavilySearchInput(query="openharness"),
        ToolExecutionContext(cwd=tmp_path),
    )

    assert result.is_error is False
    assert _FakeClient.captured["headers"]["Authorization"] == "Bearer tvly-stored"


@pytest.mark.asyncio
async def test_invalid_key_reports_401(monkeypatch, tmp_path: Path) -> None:
    module = _load_tool_module()
    _isolate_credentials(monkeypatch, tmp_path)
    monkeypatch.setenv("TAVILY_API_KEY", "bad-key")
    monkeypatch.setattr(module.httpx, "AsyncClient", _FakeClient)
    _FakeClient.response = _FakeResponse(status_code=401)
    tool = module.TavilySearchTool()

    result = await tool.execute(
        module.TavilySearchInput(query="openharness"),
        ToolExecutionContext(cwd=tmp_path),
    )

    assert result.is_error is True
    assert "401" in result.output
    assert "TAVILY_API_KEY" in result.output
