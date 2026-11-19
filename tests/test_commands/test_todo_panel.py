"""`/todos` changes only the TUI panel shape, never the provider payload."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from openharness.commands.registry import create_default_command_registry
from openharness.commands.types import CommandContext
from openharness.config.settings import load_settings
from openharness.state import AppState, AppStateStore


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))


def _context() -> CommandContext:
    # The handler never touches the engine: it only edits UI settings.
    return CommandContext(
        engine=cast(Any, None),
        cwd=".",
        app_state=AppStateStore(
            AppState(model="test", permission_mode="default", theme="default")
        ),
    )


def _handler():
    return create_default_command_registry().lookup("/todos")[0]


def test_todos_command_is_busy_safe():
    command = _handler()
    assert command.busy_safe is True
    assert "todo panel" in command.description.lower()


@pytest.mark.asyncio
async def test_todos_cycles_full_compact_hidden_and_persists():
    command = _handler()
    context = _context()

    assert "Todo panel: compact" in (await command.handler("", context)).message
    assert load_settings().todo_panel == "compact"

    assert "Todo panel: hidden" in (await command.handler("", context)).message
    assert load_settings().todo_panel == "hidden"

    assert "Todo panel: full" in (await command.handler("", context)).message
    assert load_settings().todo_panel == "full"


@pytest.mark.asyncio
async def test_todos_accepts_an_explicit_mode():
    command = _handler()
    context = _context()

    result = await command.handler("hidden", context)

    assert "Todo panel: hidden" in result.message
    assert load_settings().todo_panel == "hidden"
    assert context.app_state is not None
    assert context.app_state.get().todo_panel == "hidden"


@pytest.mark.asyncio
async def test_todos_show_and_invalid_arguments_do_not_write_settings():
    command = _handler()
    context = _context()

    shown = await command.handler("show", context)
    assert shown.message.startswith("Todo panel: full")

    rejected = await command.handler("sideways", context)
    assert rejected.message.startswith("Usage: /todos")
    assert load_settings().todo_panel == "full"


def _context_with_plan(plan_file: Path) -> CommandContext:
    engine = SimpleNamespace(tool_metadata={"plan_file": str(plan_file)})
    context = _context()
    context.engine = cast(Any, engine)
    return context


@pytest.mark.asyncio
async def test_todos_clear_removes_items_but_keeps_plan_prose(tmp_path):
    plan_file = tmp_path / "plan-sess.md"
    plan_file.write_text(
        "# Plan\n\nSome prose the model wrote.\n\n- [x] done item\n- [ ] open item\n",
        encoding="utf-8",
    )

    result = await _handler().handler("clear", _context_with_plan(plan_file))

    assert result.message == "Cleared 2 checklist items."
    text = plan_file.read_text(encoding="utf-8")
    assert "- [" not in text
    assert "Some prose the model wrote." in text
    # The panel is painted from the file, and the panel shape is untouched.
    assert load_settings().todo_panel == "full"


@pytest.mark.asyncio
async def test_todos_clear_without_a_plan_file_is_a_no_op():
    result = await _handler().handler("clear", _context())

    assert result.message == "No session checklist to clear."


@pytest.mark.asyncio
async def test_todos_clear_reports_an_empty_checklist(tmp_path):
    plan_file = tmp_path / "plan-sess.md"
    plan_file.write_text("# Plan\n\n- [x] all done\n", encoding="utf-8")
    context = _context_with_plan(plan_file)
    await _handler().handler("clear", context)

    again = await _handler().handler("clear", context)

    assert "already empty" in again.message
