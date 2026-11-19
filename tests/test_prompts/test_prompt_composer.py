from __future__ import annotations

from pathlib import Path

import pytest

from openharness.config.settings import Settings
from openharness.prompts.composer import PromptComposer, PromptSection
from openharness.prompts.context import (
    build_runtime_system_prompt,
    build_runtime_system_prompt_result,
)


@pytest.fixture(autouse=True)
def _clear_coordinator_mode(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_COORDINATOR_MODE", raising=False)


def test_prompt_composer_sorts_sections_and_reports_diagnostics():
    result = PromptComposer().compose(
        [
            PromptSection(name="late", content="late", order=20),
            PromptSection(name="base", content="base", order=0, required=True),
            PromptSection(name="middle", content="middle", order=10, cacheable=True),
        ]
    )

    assert result.text == "base\n\nmiddle\n\nlate"
    assert [section["name"] for section in result.sections] == [
        "base",
        "middle",
        "late",
    ]
    assert result.sections[1]["cacheable"] is True


def test_prompt_composer_tracks_contiguous_cacheable_prefix():
    result = PromptComposer().compose(
        [
            PromptSection(name="base", content="base", order=0, cacheable=True),
            PromptSection(name="profile", content="profile", order=5, cacheable=True),
            PromptSection(name="permission", content="permission", order=10),
            PromptSection(name="late", content="late", order=20, cacheable=True),
        ]
    )

    assert result.cacheable_prefix == "base\n\nprofile"
    assert result.text == "base\n\nprofile\n\npermission\n\nlate"


def test_prompt_composer_enforces_total_budget():
    composer = PromptComposer(max_total_chars=1_000)
    result = composer.compose(
        [
            PromptSection(name="base", content="x" * 400, order=0, required=True),
            PromptSection(name="optional", content="y" * 600, order=10),
        ]
    )

    assert len(result.text) <= 1_000
    assert "truncated by prompt budget" in result.text
    assert result.sections[-1]["truncated"] is True


def test_runtime_prompt_result_reports_section_diagnostics(tmp_path: Path):
    result = build_runtime_system_prompt_result(
        Settings(),
        cwd=tmp_path,
        available_tools={"agent"},
    )

    names = [section["name"] for section in result.sections]
    assert names[0] == "base"
    assert "personality" in names
    assert "model_profile" in names
    assert "delegation" in names
    assert all("chars" in section for section in result.sections)
    assert result.text.startswith("You are Shim")
    assert "# Persona: Shim" in result.text


def test_runtime_prompt_explains_deferred_tools_in_core_profile(tmp_path: Path):
    core = build_runtime_system_prompt(Settings(tool_profile="core"), cwd=tmp_path)
    assert "# Tool Discovery" in core
    assert "tool_search" in core

    full = build_runtime_system_prompt(Settings(tool_profile="full"), cwd=tmp_path)
    assert "# Tool Discovery" not in full


def test_runtime_prompt_personality_can_be_disabled_or_customized(tmp_path: Path):
    disabled = build_runtime_system_prompt(
        Settings(personality="off"),
        cwd=tmp_path,
    )
    assert "# Persona:" not in disabled

    custom = build_runtime_system_prompt(
        Settings(personality="custom", personality_custom="Answer only in haiku."),
        cwd=tmp_path,
    )
    assert "# Persona: Custom" in custom
    assert "Answer only in haiku." in custom


def test_runtime_prompt_skips_delegation_without_agent_tool(tmp_path: Path):
    prompt = build_runtime_system_prompt(
        Settings(),
        cwd=tmp_path,
        available_tools={"read_file"},
    )

    assert "Delegation And Subagents" not in prompt


def test_runtime_prompt_includes_codex_subagent_when_enabled(tmp_path: Path):
    prompt = build_runtime_system_prompt(
        Settings(),
        cwd=tmp_path,
        available_tools={"agent", "subagent_codex"},
    )

    assert "Delegation And Subagents" in prompt
    assert "subagent_codex" in prompt


def test_runtime_prompt_adds_provider_profile(tmp_path: Path):
    openai_prompt = build_runtime_system_prompt(
        Settings(api_format="openai"),
        cwd=tmp_path,
    )
    codex_prompt = build_runtime_system_prompt(
        Settings(provider="openai_codex"),
        cwd=tmp_path,
    )
    anthropic_prompt = build_runtime_system_prompt(Settings(), cwd=tmp_path)

    assert "OpenAI-compatible provider" in openai_prompt
    assert "Codex/Responses provider" in codex_prompt
    assert "Anthropic/Claude provider" in anthropic_prompt


def test_cursor_glob_rules_filter_by_active_files(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    rules = tmp_path / ".cursor" / "rules"
    rules.mkdir(parents=True)
    (rules / "ts.mdc").write_text(
        "---\n"
        'globs: ["src/**/*.ts"]\n'
        "alwaysApply: false\n"
        "---\n"
        "TS RULE\n",
        encoding="utf-8",
    )
    (rules / "docs.mdc").write_text(
        "---\n"
        'globs: ["docs/**/*.md"]\n'
        "alwaysApply: false\n"
        "---\n"
        "DOC RULE\n",
        encoding="utf-8",
    )
    (rules / "always.mdc").write_text(
        "---\nalwaysApply: true\n---\nALWAYS RULE\n",
        encoding="utf-8",
    )

    ts_prompt = build_runtime_system_prompt(
        Settings(),
        cwd=tmp_path,
        active_files={"src/app.ts"},
    )
    doc_prompt = build_runtime_system_prompt(
        Settings(),
        cwd=tmp_path,
        active_files={"docs/readme.md"},
    )
    empty_prompt = build_runtime_system_prompt(
        Settings(),
        cwd=tmp_path,
        active_files=set(),
    )

    assert "TS RULE" in ts_prompt
    assert "ALWAYS RULE" in ts_prompt
    assert "DOC RULE" not in ts_prompt
    assert "DOC RULE" in doc_prompt
    assert "TS RULE" not in doc_prompt
    assert "ALWAYS RULE" in empty_prompt
    assert "TS RULE" not in empty_prompt
    assert "DOC RULE" not in empty_prompt


def test_runtime_prompt_loads_claude_agents_and_cursor_rules(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "CLAUDE.md").write_text("claude project rule", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("agents project rule", encoding="utf-8")
    cursor_rules = tmp_path / ".cursor" / "rules"
    cursor_rules.mkdir(parents=True)
    (cursor_rules / "style.mdc").write_text(
        "---\n"
        "description: TypeScript style\n"
        'globs: ["src/**/*.ts"]\n'
        "alwaysApply: false\n"
        "---\n"
        "cursor rule body\n",
        encoding="utf-8",
    )
    (tmp_path / ".cursorrules").write_text("legacy cursor rule", encoding="utf-8")

    prompt = build_runtime_system_prompt(Settings(), cwd=tmp_path)

    assert "claude project rule" in prompt
    assert "agents project rule" in prompt
    assert "TypeScript style" in prompt
    assert "src/**/*.ts" in prompt
    assert "cursor rule body" in prompt
    assert "legacy cursor rule" in prompt


def test_prompt_result_reports_section_hashes(tmp_path: Path):
    result = build_runtime_system_prompt_result(Settings(), cwd=tmp_path)

    assert result.section_hashes
    names = [name for name, _ in result.section_hashes]
    assert names[0] == "base"
    assert all(len(digest) == 12 for _, digest in result.section_hashes)


def test_runtime_toggles_stay_out_of_the_cacheable_prefix(tmp_path: Path):
    base = build_runtime_system_prompt_result(Settings(), cwd=tmp_path)
    plan = build_runtime_system_prompt_result(
        Settings(permission={"mode": "plan"}), cwd=tmp_path
    )
    fast = build_runtime_system_prompt_result(Settings(fast_mode=True), cwd=tmp_path)
    effort = build_runtime_system_prompt_result(Settings(effort="high"), cwd=tmp_path)

    # The stable prefix is byte-identical; only the tail changes.
    assert plan.cacheable_prefix == base.cacheable_prefix
    assert fast.cacheable_prefix == base.cacheable_prefix
    assert effort.cacheable_prefix == base.cacheable_prefix
    assert plan.text != base.text
    assert fast.text != base.text
    assert effort.text != base.text


def test_live_environment_is_the_last_section(tmp_path: Path):
    result = build_runtime_system_prompt_result(Settings(), cwd=tmp_path)

    assert result.sections[-1]["name"] == "environment_live"
    assert result.sections[-1]["cacheable"] is False
    live_index = result.text.index("# Live Context")
    assert live_index > result.text.index("# Environment")
    assert "Working directory" in result.text[live_index:]


def test_scoped_cursor_rules_live_in_the_tail(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    rules = tmp_path / ".cursor" / "rules"
    rules.mkdir(parents=True)
    (rules / "ts.mdc").write_text(
        "---\n"
        'globs: ["src/**/*.ts"]\n'
        "alwaysApply: false\n"
        "---\n"
        "TS RULE\n",
        encoding="utf-8",
    )
    (rules / "always.mdc").write_text(
        "---\nalwaysApply: true\n---\nALWAYS RULE\n",
        encoding="utf-8",
    )

    with_ts = build_runtime_system_prompt_result(
        Settings(), cwd=tmp_path, active_files={"src/app.ts"}
    )
    without_ts = build_runtime_system_prompt_result(
        Settings(), cwd=tmp_path, active_files=set()
    )

    assert with_ts.cacheable_prefix == without_ts.cacheable_prefix
    assert "ALWAYS RULE" in with_ts.cacheable_prefix
    assert "TS RULE" not in with_ts.cacheable_prefix
    assert "TS RULE" in with_ts.text
    assert "TS RULE" not in without_ts.text
    scoped = [s for s in with_ts.sections if s["name"] == "project_rules_scoped"]
    assert scoped and scoped[0]["cacheable"] is False


def test_stable_prompt_ignores_turn_settings(tmp_path: Path):
    from openharness.prompts.context import (
        build_stable_system_prompt_result,
        build_turn_context_result,
    )

    base = build_stable_system_prompt_result(Settings(), cwd=tmp_path)
    plan = build_stable_system_prompt_result(
        Settings(permission={"mode": "plan"}, fast_mode=True, effort="high"),
        cwd=tmp_path,
    )
    base_turn = build_turn_context_result(Settings(), cwd=tmp_path)
    plan_turn = build_turn_context_result(
        Settings(permission={"mode": "plan"}, fast_mode=True, effort="high"),
        cwd=tmp_path,
    )

    assert plan.text == base.text
    assert plan.cacheable_prefix == base.cacheable_prefix
    assert plan.section_hashes == base.section_hashes
    assert plan_turn.text != base_turn.text
    assert "Plan mode is enabled" in plan_turn.text
    assert "Fast mode is enabled" in plan_turn.text
    assert "Effort: high" in plan_turn.text


def test_turn_context_contains_live_context_and_scoped_rules(
    tmp_path: Path, monkeypatch
):
    from openharness.prompts.context import build_turn_context

    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    rules = tmp_path / ".cursor" / "rules"
    rules.mkdir(parents=True)
    (rules / "ts.mdc").write_text(
        "---\n"
        'globs: ["src/**/*.ts"]\n'
        "alwaysApply: false\n"
        "---\n"
        "TS RULE\n",
        encoding="utf-8",
    )

    context = build_turn_context(
        Settings(), cwd=tmp_path, active_files={"src/app.ts"}
    )

    assert "# Live Context" in context
    assert "Working directory" in context
    assert "TS RULE" in context
    assert "Current Permission Mode" in context


def test_full_prompt_is_stable_prefix_plus_turn_context(tmp_path: Path):
    from openharness.prompts.context import (
        build_stable_system_prompt_result,
        build_turn_context_result,
    )

    settings = Settings(permission={"mode": "plan"})
    stable = build_stable_system_prompt_result(settings, cwd=tmp_path)
    turn = build_turn_context_result(
        settings, cwd=tmp_path, latest_user_prompt="fix it"
    )
    full = build_runtime_system_prompt_result(
        settings, cwd=tmp_path, latest_user_prompt="fix it"
    )

    assert full.cacheable_prefix == stable.cacheable_prefix
    assert full.text == f"{stable.text}\n\n{turn.text}"
    assert full.section_hashes == stable.section_hashes + turn.section_hashes
