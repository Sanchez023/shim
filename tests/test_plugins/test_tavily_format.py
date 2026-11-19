"""Tests for the Tavily plugin's context-budget caps."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_tavily_module():
    path = (
        Path(__file__).resolve().parents[2]
        / "plugins"
        / "tavily-search"
        / "tools"
        / "tavily_search.py"
    )
    spec = importlib.util.spec_from_file_location("tavily_search_tool", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_tavily_caps_total_output():
    module = _load_tavily_module()
    payload = {
        "answer": "a" * 20_000,
        "results": [{"title": "t", "url": "https://example.com", "content": "short"}],
    }

    output = module.format_tavily_results("q", payload)

    assert len(output) <= 8_000 + len("\n... (truncated)")
    assert output.endswith("... (truncated)")


def test_tavily_caps_each_snippet():
    module = _load_tavily_module()
    payload = {
        "results": [
            {"title": "t", "url": "https://example.com", "content": "c" * 2_000},
        ],
    }

    output = module.format_tavily_results("q", payload)
    snippet = next(line.strip() for line in output.splitlines() if line.strip().startswith("c"))

    assert len(snippet) == 400
