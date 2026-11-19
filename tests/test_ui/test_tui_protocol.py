from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from openharness.state.app_state import AppState
from openharness.ui.backend_host import BackendHostConfig, ReactBackendHost
from openharness.ui.protocol import BackendEvent, FrontendRequest


def test_frontend_request_accepts_search_and_shell():
    search = FrontendRequest.model_validate({"type": "search_files", "query": "app"})
    shell = FrontendRequest.model_validate({"type": "run_shell", "line": "!ls"})

    assert search.query == "app"
    assert shell.line == "!ls"


def test_ready_event_includes_command_descriptions():
    event = BackendEvent.ready(
        AppState(model="test", permission_mode="default", theme="default"),
        [],
        ["/help"],
        {"/help": "Show help"},
    )

    assert event.command_descriptions == {"/help": "Show help"}


def test_ready_event_carries_the_busy_safe_command_list():
    event = BackendEvent.ready(
        AppState(model="test", permission_mode="default", theme="default"),
        [],
        ["/help", "/status"],
        {"/help": "Show help", "/status": "Show session status"},
        ["/status"],
    )

    assert event.busy_safe_commands == ["/status"]


def test_ready_event_defaults_to_no_busy_safe_commands():
    event = BackendEvent.ready(
        AppState(model="test", permission_mode="default", theme="default"),
        [],
        ["/help"],
        {"/help": "Show help"},
    )

    assert event.busy_safe_commands == []


def test_state_payload_exposes_the_todo_panel_shape():
    event = BackendEvent.ready(
        AppState(
            model="test",
            permission_mode="default",
            theme="default",
            todo_panel="compact",
        ),
        [],
        ["/todos"],
        {"/todos": "Show or set the todo panel"},
    )

    assert event.state is not None
    assert event.state["todo_panel"] == "compact"


def test_state_payload_defaults_the_todo_panel_to_full():
    event = BackendEvent.ready(
        AppState(model="test", permission_mode="default", theme="default"),
        [],
        [],
    )

    assert event.state is not None
    assert event.state["todo_panel"] == "full"


def test_file_search_results_event():
    event = BackendEvent(type="file_search_results", files=["src/app.py"])

    assert event.files == ["src/app.py"]


@pytest.mark.asyncio
async def test_run_shell_command_emits_tool_events(tmp_path, monkeypatch):
    host = ReactBackendHost(BackendHostConfig(cwd=str(tmp_path)))
    host._bundle = SimpleNamespace(cwd=str(tmp_path))
    emitted: list[BackendEvent] = []

    async def fake_emit(event: BackendEvent) -> None:
        emitted.append(event)

    monkeypatch.setattr(host, "_emit", fake_emit)
    monkeypatch.setattr(
        host,
        "_status_snapshot",
        lambda: BackendEvent(type="state_snapshot"),
    )

    await host._run_shell_command("!echo shell-ok")

    assert emitted[0].item is not None
    assert emitted[0].item.role == "user"
    completed = [event for event in emitted if event.type == "tool_completed"]
    assert completed
    assert "shell-ok" in (completed[0].output or "")
    assert emitted[-1].type == "line_complete"


def test_warm_sdk_imports_the_active_provider_module(monkeypatch):
    """`ready` must not wait for the vendor SDK, so it is warmed in a thread."""
    from openharness.api.openai_client import OpenAICompatibleClient
    from openharness.ui import backend_host as bh

    host = ReactBackendHost(BackendHostConfig())
    host._bundle = SimpleNamespace(
        api_client=OpenAICompatibleClient(api_key="test-key"),
    )
    warmed: list[str] = []
    monkeypatch.setattr(bh, "_import_module_quietly", warmed.append)

    host._warm_sdk()

    deadline = time.monotonic() + 2.0
    while not warmed and time.monotonic() < deadline:
        time.sleep(0.01)
    assert warmed == ["openai"]


def test_warm_sdk_ignores_clients_without_a_vendor_module(monkeypatch):
    from openharness.ui import backend_host as bh

    host = ReactBackendHost(BackendHostConfig())
    host._bundle = SimpleNamespace(api_client=object())
    warmed: list[str] = []
    monkeypatch.setattr(bh, "_import_module_quietly", warmed.append)

    host._warm_sdk()

    time.sleep(0.05)
    assert warmed == []


def test_import_module_quietly_swallows_failures():
    from openharness.ui.backend_host import _import_module_quietly

    _import_module_quietly("openharness_does_not_exist_abc")
