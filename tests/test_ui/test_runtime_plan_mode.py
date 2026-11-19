from __future__ import annotations

from pathlib import Path

import pytest

from openharness.permissions import PermissionMode
from openharness.runtime.seams.governance.models import SecurityRequest
from openharness.ui.runtime import build_runtime, close_runtime, handle_line


class _StaticApiClient:
    async def stream_message(self, request):
        del request
        if False:
            yield None


@pytest.mark.asyncio
async def test_plan_command_refreshes_engine_system_prompt(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OPENHARNESS_LOGS_DIR", str(tmp_path / "logs"))

    bundle = await build_runtime(cwd=str(tmp_path), api_client=_StaticApiClient())
    try:
        assert "Plan mode is enabled" not in bundle.engine.turn_context

        async def print_system(text: str) -> None:
            del text

        async def render_event(event) -> None:
            del event

        async def clear_output() -> None:
            pass

        should_continue = await handle_line(
            bundle,
            "/plan on",
            print_system=print_system,
            render_event=render_event,
            clear_output=clear_output,
        )

        assert should_continue is True
        assert bundle.app_state.get().permission_mode == "plan"
        assert "Plan mode is enabled" in bundle.engine.turn_context
        assert "Do not call mutating tools" in bundle.engine.turn_context
        assert "ask_user_question" in bundle.engine.turn_context
    finally:
        await close_runtime(bundle)


async def _noop_print_system(text: str) -> None:
    del text


async def _noop_render_event(event) -> None:
    del event


async def _noop_clear_output() -> None:
    return None


async def _security_decision(bundle, tool_name: str) -> bool:
    assert bundle.engine.security_policy is not None
    decision = await bundle.engine.security_policy.evaluate(
        SecurityRequest(tool_name=tool_name, is_read_only=False)
    )
    return decision.allowed


@pytest.mark.asyncio
async def test_agent_plan_switch_updates_live_checker_and_security_policy(
    tmp_path: Path, monkeypatch
):
    """enter_plan_mode/exit_plan_mode must change the *live* runtime.

    The security policy captures the checker object by reference, so this also
    guards against replacing the checker with a fresh object on mode switches.
    """
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OPENHARNESS_LOGS_DIR", str(tmp_path / "logs"))

    bundle = await build_runtime(
        cwd=str(tmp_path),
        api_client=_StaticApiClient(),
        permission_mode="full_auto",
    )
    try:
        set_mode = bundle.engine.tool_metadata["set_permission_mode"]

        set_mode("plan")
        assert bundle.engine.permission_mode == PermissionMode.PLAN
        assert bundle.app_state.get().permission_mode == "plan"
        assert "Plan mode is enabled" in bundle.engine.turn_context
        assert await _security_decision(bundle, "write_file") is False

        set_mode("full_auto")
        assert bundle.engine.permission_mode == PermissionMode.FULL_AUTO
        assert bundle.app_state.get().permission_mode == "full_auto"
        assert "Full-auto permission mode is enabled" in bundle.engine.turn_context
        assert await _security_decision(bundle, "write_file") is True
    finally:
        await close_runtime(bundle)


@pytest.mark.asyncio
async def test_plan_slash_commands_mutate_the_live_checker(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OPENHARNESS_LOGS_DIR", str(tmp_path / "logs"))

    bundle = await build_runtime(cwd=str(tmp_path), api_client=_StaticApiClient())
    try:
        await handle_line(
            bundle,
            "/plan on",
            print_system=_noop_print_system,
            render_event=_noop_render_event,
            clear_output=_noop_clear_output,
        )
        assert bundle.engine.permission_mode == PermissionMode.PLAN
        assert await _security_decision(bundle, "write_file") is False

        await handle_line(
            bundle,
            "/plan off",
            print_system=_noop_print_system,
            render_event=_noop_render_event,
            clear_output=_noop_clear_output,
        )
        # Exiting plan mode resumes in Auto, matching exit_plan_mode.
        assert bundle.engine.permission_mode == PermissionMode.FULL_AUTO
        assert bundle.app_state.get().permission_mode == "full_auto"
        assert await _security_decision(bundle, "write_file") is True
    finally:
        await close_runtime(bundle)
