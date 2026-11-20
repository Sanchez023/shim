"""Tests for slash command handlers."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from openharness.api.client import ApiMessageCompleteEvent
from openharness.api.usage import UsageSnapshot
from openharness.autopilot import RepoVerificationStep
from openharness.commands.registry import (
    CommandContext,
    MemoryCommandBackend,
    create_default_command_registry,
    lookup_skill_slash_command,
)
from openharness.config.paths import (
    get_feedback_log_path,
    get_project_issue_file,
    get_project_pr_comments_file,
)
from openharness.config.settings import Settings, load_settings, save_settings
from openharness.engine.messages import ConversationMessage, TextBlock, ThinkingBlock
from openharness.engine.query_engine import QueryEngine
from openharness.mcp.types import McpHttpServerConfig, McpStdioServerConfig
from openharness.memory.paths import get_project_memory_dir
from openharness.permissions import PermissionChecker
from openharness.plugins.types import PluginCommandDefinition
from openharness.services.session_storage import get_project_session_dir
from openharness.state import AppState, AppStateStore
from openharness.tasks import get_task_manager
from openharness.tools import create_default_tool_registry
from openharness.version import __version__


class FakeApiClient:
    async def stream_message(self, request):
        del request
        raise AssertionError("stream_message should not be called in command tests")


def _make_engine(tmp_path: Path) -> QueryEngine:
    return QueryEngine(
        api_client=FakeApiClient(),
        tool_registry=create_default_tool_registry(),
        permission_checker=PermissionChecker(load_settings().permission),
        cwd=tmp_path,
        model="claude-test",
        system_prompt="system",
    )


def _make_context(tmp_path: Path) -> CommandContext:
    tool_registry = create_default_tool_registry()
    return CommandContext(
        engine=QueryEngine(
            api_client=FakeApiClient(),
            tool_registry=tool_registry,
            permission_checker=PermissionChecker(load_settings().permission),
            cwd=tmp_path,
            model="claude-test",
            system_prompt="system",
        ),
        cwd=str(tmp_path),
        tool_registry=tool_registry,
        app_state=AppStateStore(
            AppState(
                model="claude-test",
                permission_mode="default",
                theme="default",
                keybindings={},
            )
        ),
    )


def _init_git_repo(path: Path) -> None:
    """Create a git repository with a committer identity at *path*.

    ``subprocess`` blocks, so the git plumbing the ``async`` test needs lives in
    a plain synchronous helper instead of inside the coroutine.
    """
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "config", "user.email", "openharness@example.com"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "OpenHarness Tests"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.mark.asyncio
async def test_permissions_command_persists(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    registry = create_default_command_registry()
    command, args = registry.lookup("/permissions set full_auto")
    assert command is not None

    result = await command.handler(args, CommandContext(engine=_make_engine(tmp_path), cwd=str(tmp_path)))

    assert "Auto" in result.message
    assert load_settings().permission.mode == "full_auto"


@pytest.mark.asyncio
async def test_permissions_command_is_marked_local_only(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    registry = create_default_command_registry()
    command, _ = registry.lookup("/permissions set full_auto")
    assert command is not None
    assert command.remote_invocable is False


@pytest.mark.asyncio
async def test_permissions_command_supports_explicit_remote_admin_opt_in(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    registry = create_default_command_registry()
    command, _ = registry.lookup("/permissions set full_auto")
    assert command is not None
    assert getattr(command, "remote_admin_opt_in", False) is True


@pytest.mark.asyncio
async def test_stop_command_explains_interrupt_paths(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    registry = create_default_command_registry()
    command, args = registry.lookup("/stop")
    assert command is not None

    result = await command.handler(args, CommandContext(engine=_make_engine(tmp_path), cwd=str(tmp_path)))

    assert "/stop" in result.message
    assert "Esc/Ctrl+C" in result.message


@pytest.mark.asyncio
async def test_plugin_command_is_marked_local_only(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    registry = create_default_command_registry()
    command, _ = registry.lookup("/plugin list")
    assert command is not None
    assert command.remote_invocable is False


@pytest.mark.asyncio
async def test_plugin_command_supports_explicit_remote_admin_opt_in(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    registry = create_default_command_registry()
    command, _ = registry.lookup("/plugin list")
    assert command is not None
    assert getattr(command, "remote_admin_opt_in", False) is True


@pytest.mark.asyncio
async def test_reload_plugins_command_is_marked_local_only(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    registry = create_default_command_registry()
    command, _ = registry.lookup("/reload-plugins")
    assert command is not None
    assert command.remote_invocable is False


@pytest.mark.asyncio
async def test_reload_plugins_command_supports_explicit_remote_admin_opt_in(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    registry = create_default_command_registry()
    command, _ = registry.lookup("/reload-plugins")
    assert command is not None
    assert getattr(command, "remote_admin_opt_in", False) is True


@pytest.mark.asyncio
async def test_bridge_command_is_marked_local_only(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    registry = create_default_command_registry()
    command, _ = registry.lookup("/bridge spawn id")
    assert command is not None
    assert command.remote_invocable is False


@pytest.mark.asyncio
async def test_bridge_command_supports_explicit_remote_admin_opt_in(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    registry = create_default_command_registry()
    command, _ = registry.lookup("/bridge spawn id")
    assert command is not None
    assert getattr(command, "remote_admin_opt_in", False) is True


@pytest.mark.asyncio
async def test_autopilot_command_is_marked_local_only(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    registry = create_default_command_registry()
    command, _ = registry.lookup("/autopilot run-next")
    assert command is not None
    assert command.remote_invocable is False


@pytest.mark.asyncio
async def test_autopilot_command_supports_explicit_remote_admin_opt_in(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    registry = create_default_command_registry()
    command, _ = registry.lookup("/autopilot run-next")
    assert command is not None
    assert getattr(command, "remote_admin_opt_in", False) is True


@pytest.mark.asyncio
async def test_diff_command_is_marked_local_only(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    registry = create_default_command_registry()
    command, _ = registry.lookup("/diff full")
    assert command is not None
    assert command.remote_invocable is False


@pytest.mark.asyncio
async def test_diff_command_supports_explicit_remote_admin_opt_in(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    registry = create_default_command_registry()
    command, _ = registry.lookup("/diff full")
    assert command is not None
    assert getattr(command, "remote_admin_opt_in", False) is True


@pytest.mark.asyncio
async def test_project_context_commands_are_marked_local_only(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    registry = create_default_command_registry()

    for payload in (
        "/issue set Remote supplied issue :: marker",
        "/pr_comments add src/app.py:1 :: marker",
    ):
        command, _ = registry.lookup(payload)
        assert command is not None
        assert command.remote_invocable is False, payload


@pytest.mark.asyncio
async def test_project_context_commands_support_explicit_remote_admin_opt_in(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    registry = create_default_command_registry()

    for payload in (
        "/issue set Remote supplied issue :: marker",
        "/pr_comments add src/app.py:1 :: marker",
    ):
        command, _ = registry.lookup(payload)
        assert command is not None
        assert getattr(command, "remote_admin_opt_in", False) is True, payload


@pytest.mark.asyncio
async def test_tasks_command_is_marked_local_only(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    registry = create_default_command_registry()
    command, _ = registry.lookup("/tasks run id")
    assert command is not None
    assert command.remote_invocable is False


@pytest.mark.asyncio
async def test_tasks_command_supports_explicit_remote_admin_opt_in(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    registry = create_default_command_registry()
    command, _ = registry.lookup("/tasks run id")
    assert command is not None
    assert getattr(command, "remote_admin_opt_in", False) is True


@pytest.mark.asyncio
async def test_sensitive_control_plane_commands_are_local_only(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    registry = create_default_command_registry()

    for payload in (
        "/config show",
        "/login TEST_KEY",
        "/logout",
        "/mcp",
        "/provider",
        "/model show",
        "/commit remote requested commit",
        "/ship",
        "/resume",
        "/resume session-from-another-sender",
        "/summary 10",
    ):
        command, _ = registry.lookup(payload)
        assert command is not None
        assert command.remote_invocable is False, payload
        assert command.remote_admin_opt_in is True, payload


@pytest.mark.asyncio
async def test_config_show_redacts_nested_mcp_and_vision_secrets(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    settings = Settings(
        api_key="TOP_LEVEL_FAKE_SECRET",
        mcp_servers={
            "internal-http": McpHttpServerConfig(
                url="https://mcp.internal",
                headers={
                    "Authorization": "Bearer MCP_FAKE_SECRET",
                    "X-Token": "RAW_FAKE_TOKEN",
                    "X-Public": "non-secret-value",
                },
            ),
            "local-stdio": McpStdioServerConfig(
                command="server",
                env={
                    "MCP_AUTH_TOKEN": "STDIO_FAKE_SECRET",
                    "SAFE_SETTING": "visible",
                },
            ),
        },
        vision={
            "model": "vision-test",
            "api_key": "VISION_FAKE_SECRET",
            "base_url": "https://vision.example",
        },
    )
    monkeypatch.setattr(
        "openharness.commands.builtin.settings.load_settings", lambda: settings
    )

    registry = create_default_command_registry()
    command, args = registry.lookup("/config show")
    assert command is not None

    result = await command.handler(args, CommandContext(engine=_make_engine(tmp_path), cwd=str(tmp_path)))

    for secret in (
        "TOP_LEVEL_FAKE_SECRET",
        "MCP_FAKE_SECRET",
        "RAW_FAKE_TOKEN",
        "STDIO_FAKE_SECRET",
        "VISION_FAKE_SECRET",
    ):
        assert secret not in result.message
    assert "[REDACTED]" in result.message
    assert "non-secret-value" in result.message
    assert "visible" in result.message


@pytest.mark.asyncio
async def test_memory_show_rejects_path_traversal(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    registry = create_default_command_registry()
    command, args = registry.lookup("/memory show ../../../../../../etc/hosts")
    assert command is not None

    result = await command.handler(args, CommandContext(engine=_make_engine(tmp_path), cwd=str(tmp_path)))

    assert result.message == "Memory entry path must stay within the configured memory directory."


@pytest.mark.asyncio
async def test_memory_show_reads_normal_entries_with_md_fallback(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    registry = create_default_command_registry()

    add_command, add_args = registry.lookup("/memory add Notes :: hello world")
    assert add_command is not None
    await add_command.handler(add_args, CommandContext(engine=_make_engine(tmp_path), cwd=str(tmp_path)))

    show_command, show_args = registry.lookup("/memory show Notes")
    result = await show_command.handler(show_args, CommandContext(engine=_make_engine(tmp_path), cwd=str(tmp_path)))

    assert "hello world" in result.message


@pytest.mark.asyncio
async def test_model_command_persists(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    registry = create_default_command_registry()
    command, args = registry.lookup("/model opus")
    assert command is not None

    result = await command.handler(args, CommandContext(engine=_make_engine(tmp_path), cwd=str(tmp_path)))

    assert "opus" in result.message
    assert load_settings().resolve_profile()[1].last_model == "opus"
    assert load_settings().model == "claude-opus-4-6"


@pytest.mark.asyncio
async def test_model_command_accepts_direct_value(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    registry = create_default_command_registry()
    command, args = registry.lookup("/model gpt-5.4")
    assert command is not None

    result = await command.handler(args, CommandContext(engine=_make_engine(tmp_path), cwd=str(tmp_path)))

    assert "gpt-5.4" in result.message
    assert load_settings().model == "gpt-5.4"


@pytest.mark.asyncio
async def test_model_command_lists_profile_model_allowlist(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    save_settings(
        Settings().model_copy(
            update={
                "active_profile": "local-llm",
                "provider": "openai",
                "api_format": "openai",
                "base_url": "http://localhost:8000/v1",
                "model": "deepseek-chat",
                "profiles": {
                    "local-llm": {
                        "label": "Local LLM",
                        "provider": "openai",
                        "api_format": "openai",
                        "auth_source": "openai_api_key",
                        "default_model": "deepseek-chat",
                        "last_model": "deepseek-chat",
                        "base_url": "http://localhost:8000/v1",
                        "allowed_models": ["deepseek-chat", "qwen-vl"],
                    }
                },
            }
        )
    )
    registry = create_default_command_registry()
    command, args = registry.lookup("/model list")
    assert command is not None

    result = await command.handler(args, CommandContext(engine=_make_engine(tmp_path), cwd=str(tmp_path)))

    assert "Switchable models for profile 'local-llm'" in result.message
    assert "- deepseek-chat" in result.message
    assert "- qwen-vl" in result.message


@pytest.mark.asyncio
async def test_model_command_adds_model_to_profile_allowlist(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    save_settings(
        Settings().model_copy(
            update={
                "active_profile": "local-llm",
                "provider": "openai",
                "api_format": "openai",
                "base_url": "http://localhost:8000/v1",
                "model": "deepseek-chat",
                "profiles": {
                    "local-llm": {
                        "label": "Local LLM",
                        "provider": "openai",
                        "api_format": "openai",
                        "auth_source": "openai_api_key",
                        "default_model": "deepseek-chat",
                        "last_model": "deepseek-chat",
                        "base_url": "http://localhost:8000/v1",
                        "allowed_models": ["deepseek-chat"],
                    }
                },
            }
        )
    )
    registry = create_default_command_registry()
    command, args = registry.lookup("/model add qwen-vl")
    assert command is not None

    result = await command.handler(args, CommandContext(engine=_make_engine(tmp_path), cwd=str(tmp_path)))

    assert result.refresh_runtime is True
    assert load_settings().resolve_profile()[1].allowed_models == ["deepseek-chat", "qwen-vl"]


@pytest.mark.asyncio
async def test_model_command_remove_current_model_resets_to_default(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    save_settings(
        Settings().model_copy(
            update={
                "active_profile": "local-llm",
                "provider": "openai",
                "api_format": "openai",
                "base_url": "http://localhost:8000/v1",
                "model": "qwen-vl",
                "profiles": {
                    "local-llm": {
                        "label": "Local LLM",
                        "provider": "openai",
                        "api_format": "openai",
                        "auth_source": "openai_api_key",
                        "default_model": "deepseek-chat",
                        "last_model": "qwen-vl",
                        "base_url": "http://localhost:8000/v1",
                        "allowed_models": ["deepseek-chat", "qwen-vl"],
                    }
                },
            }
        )
    )
    registry = create_default_command_registry()
    context = _make_context(tmp_path)
    command, args = registry.lookup("/model remove qwen-vl")
    assert command is not None

    result = await command.handler(args, context)

    profile = load_settings().resolve_profile()[1]
    assert result.refresh_runtime is True
    assert profile.allowed_models == ["deepseek-chat"]
    assert profile.last_model == ""
    assert context.engine.model == "deepseek-chat"


@pytest.mark.asyncio
async def test_model_command_clear_removes_profile_allowlist(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    save_settings(
        Settings().model_copy(
            update={
                "active_profile": "local-llm",
                "provider": "openai",
                "api_format": "openai",
                "base_url": "http://localhost:8000/v1",
                "model": "deepseek-chat",
                "profiles": {
                    "local-llm": {
                        "label": "Local LLM",
                        "provider": "openai",
                        "api_format": "openai",
                        "auth_source": "openai_api_key",
                        "default_model": "deepseek-chat",
                        "last_model": "deepseek-chat",
                        "base_url": "http://localhost:8000/v1",
                        "allowed_models": ["deepseek-chat", "qwen-vl"],
                    }
                },
            }
        )
    )
    registry = create_default_command_registry()
    command, args = registry.lookup("/model clear")
    assert command is not None

    result = await command.handler(args, CommandContext(engine=_make_engine(tmp_path), cwd=str(tmp_path)))

    assert result.refresh_runtime is True
    assert load_settings().resolve_profile()[1].allowed_models == []


@pytest.mark.asyncio
async def test_model_command_default_clears_profile_override(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    save_settings(
        Settings().model_copy(
            update={
                "active_profile": "claude-api",
                "profiles": {
                    "claude-api": {
                        "label": "Claude API",
                        "provider": "anthropic",
                        "api_format": "anthropic",
                        "auth_source": "anthropic_api_key",
                        "default_model": "sonnet",
                        "last_model": "opus",
                    }
                },
            }
        )
    )
    registry = create_default_command_registry()
    command, args = registry.lookup("/model default")
    assert command is not None

    result = await command.handler(args, CommandContext(engine=_make_engine(tmp_path), cwd=str(tmp_path)))

    assert "reset to default" in result.message
    assert load_settings().resolve_profile()[1].last_model == ""
    assert load_settings().model == "claude-sonnet-4-6"


@pytest.mark.asyncio
async def test_turns_show_reports_unlimited_engine_when_session_is_unbounded(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    registry = create_default_command_registry()
    context = _make_context(tmp_path)
    context.engine.set_max_turns(None)

    command, args = registry.lookup("/turns show")
    assert command is not None

    result = await command.handler(args, context)

    assert "Max turns (engine): unlimited" in result.message


@pytest.mark.asyncio
async def test_turns_command_accepts_unlimited(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    registry = create_default_command_registry()
    context = _make_context(tmp_path)

    command, args = registry.lookup("/turns unlimited")
    assert command is not None

    result = await command.handler(args, context)

    assert "unlimited for this session" in result.message
    assert context.engine.max_turns is None


@pytest.mark.asyncio
async def test_provider_command_switches_profile_and_requests_runtime_refresh(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    save_settings(
        Settings().model_copy(
            update={
                "profiles": {
                    "kimi-anthropic": {
                        "label": "Kimi Anthropic",
                        "provider": "anthropic",
                        "api_format": "anthropic",
                        "auth_source": "anthropic_api_key",
                        "default_model": "kimi-k2.5",
                        "last_model": "kimi-k2.5",
                        "base_url": "https://api.moonshot.cn/anthropic",
                        "allowed_models": ["kimi-k2.5"],
                    }
                }
            }
        )
    )
    registry = create_default_command_registry()
    context = _make_context(tmp_path)

    command, args = registry.lookup("/provider kimi-anthropic")
    assert command is not None

    result = await command.handler(args, context)

    loaded = load_settings()
    assert result.refresh_runtime is True
    assert loaded.active_profile == "kimi-anthropic"
    assert loaded.base_url == "https://api.moonshot.cn/anthropic"
    assert loaded.model == "kimi-k2.5"


@pytest.mark.asyncio
async def test_autopilot_command_add_list_and_complete(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    registry = create_default_command_registry()
    context = _make_context(tmp_path)

    add_command, add_args = registry.lookup("/autopilot add idea Build unified queue :: intake from issues and prs")
    assert add_command is not None
    add_result = await add_command.handler(add_args, context)
    assert "Queued autopilot card" in add_result.message

    list_command, list_args = registry.lookup("/autopilot list")
    assert list_command is not None
    list_result = await list_command.handler(list_args, context)
    assert "Build unified queue" in list_result.message

    next_command, next_args = registry.lookup("/autopilot next")
    next_result = await next_command.handler(next_args, context)
    assert "Build unified queue" in next_result.message
    card_id = next_result.message.split()[0]

    complete_command, complete_args = registry.lookup(f"/autopilot complete {card_id} implemented")
    complete_result = await complete_command.handler(complete_args, context)
    assert "-> completed" in complete_result.message

    status_command, status_args = registry.lookup("/autopilot status")
    status_result = await status_command.handler(status_args, context)
    assert "- completed: 1" in status_result.message


@pytest.mark.asyncio
async def test_autopilot_command_export_dashboard(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    registry = create_default_command_registry()
    context = _make_context(tmp_path)

    command, args = registry.lookup("/autopilot export-dashboard")
    assert command is not None
    result = await command.handler(args, context)

    assert "Exported autopilot dashboard:" in result.message
    output_dir = Path(result.message.split(": ", 1)[1])
    assert (output_dir / "index.html").exists()
    assert (output_dir / "snapshot.json").exists()


@pytest.mark.asyncio
async def test_ship_command_queues_and_executes_card(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    registry = create_default_command_registry()
    context = _make_context(tmp_path)

    async def fake_run_agent_prompt(self, prompt: str, *, model, max_turns, permission_mode, cwd=None):
        return "Implemented the requested feature."

    def fake_run_verification_steps(self, policies, *, cwd=None):
        return [RepoVerificationStep(command="uv run pytest -q", returncode=0, status="success")]

    monkeypatch.setattr(
        "openharness.autopilot.service.RepoAutopilotStore._run_agent_prompt",
        fake_run_agent_prompt,
    )
    monkeypatch.setattr(
        "openharness.autopilot.service.RepoAutopilotStore._run_verification_steps",
        fake_run_verification_steps,
    )

    command, args = registry.lookup("/ship Build autopilot tick :: end-to-end automation")
    assert command is not None
    result = await command.handler(args, context)

    assert "-> completed" in result.message
    assert "run report:" in result.message
    assert "verification report:" in result.message


@pytest.mark.asyncio
async def test_plugin_command_registers_and_submits_prompt(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    registry = create_default_command_registry(
        plugin_commands=[
            PluginCommandDefinition(
                name="fixture:ops:restart",
                description="Restart services safely",
                content="Base workflow\n\n$ARGUMENTS",
                user_invocable=True,
            )
        ]
    )
    command, args = registry.lookup("/fixture:ops:restart api")
    assert command is not None

    result = await command.handler(args, _make_context(tmp_path))

    assert result.submit_prompt == "Base workflow\n\napi"


@pytest.mark.asyncio
async def test_bundled_user_invocable_skill_registers_as_slash_command(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    registry = create_default_command_registry()
    command, args = registry.lookup("/skill-creator create a deployment skill")
    assert command is not None
    assert command.name == "skill-creator"

    result = await command.handler(args, _make_context(tmp_path))

    assert result.submit_prompt is not None
    assert "Base directory for this skill:" in result.submit_prompt
    assert "# skill-creator" in result.submit_prompt
    assert "Arguments: create a deployment skill" in result.submit_prompt


@pytest.mark.asyncio
async def test_context_skill_slash_command_uses_folder_name(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    extra_root = tmp_path / "ohmo-skills"
    skill_dir = extra_root / "pikastream-video-meeting"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: PikaStream Video Meeting\n"
        "description: Join and summarize video meetings.\n"
        "---\n\n"
        "# PikaStream Video Meeting\n\n"
        "Use the meeting workflow.\n",
        encoding="utf-8",
    )
    context = _make_context(tmp_path)
    context.extra_skill_dirs = (extra_root,)

    parsed = lookup_skill_slash_command("/pikastream-video-meeting room 123", context)
    assert parsed is not None
    command, args = parsed

    result = await command.handler(args, context)

    assert command.name == "pikastream-video-meeting"
    assert result.submit_prompt is not None
    assert f"Base directory for this skill: {skill_dir.resolve()}" in result.submit_prompt
    assert "PikaStream Video Meeting" in result.submit_prompt
    assert "Arguments: room 123" in result.submit_prompt


@pytest.mark.asyncio
async def test_user_invocable_false_skill_is_not_slash_resolved(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    extra_root = tmp_path / "skills"
    skill_dir = extra_root / "hidden"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "description: Model-only helper.\n"
        "user-invocable: false\n"
        "---\n\n"
        "# Hidden\n",
        encoding="utf-8",
    )
    context = _make_context(tmp_path)
    context.extra_skill_dirs = (extra_root,)

    assert lookup_skill_slash_command("/hidden", context) is None


@pytest.mark.asyncio
async def test_project_skill_registers_as_context_slash_command(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    repo = tmp_path / "repo"
    repo.mkdir()
    skill_dir = repo / ".claude" / "skills" / "shipit"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "description: Project ship workflow.\n"
        "---\n\n"
        "# Shipit\n\nShip this repo.\n",
        encoding="utf-8",
    )
    context = _make_context(repo)

    parsed = lookup_skill_slash_command("/shipit now", context)
    assert parsed is not None
    command, args = parsed
    result = await command.handler(args, context)

    assert command.name == "shipit"
    assert result.submit_prompt is not None
    assert f"Base directory for this skill: {skill_dir.resolve()}" in result.submit_prompt
    assert "Arguments: now" in result.submit_prompt


@pytest.mark.asyncio
async def test_skills_command_lists_project_skill_path(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    repo = tmp_path / "repo"
    repo.mkdir()
    skill_dir = repo / ".agents" / "skills" / "triage"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Triage\nTriage workflow.\n", encoding="utf-8")
    registry = create_default_command_registry()
    command, args = registry.lookup("/skills")
    assert command is not None

    result = await command.handler(args, _make_context(repo))

    assert "triage (Triage) [project]" in result.message
    assert str((skill_dir / "SKILL.md").resolve()) in result.message


@pytest.mark.asyncio
async def test_disable_model_invocation_skill_still_allows_user_slash(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    extra_root = tmp_path / "skills"
    skill_dir = extra_root / "deploy"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "description: User-triggered deploy workflow.\n"
        "disable-model-invocation: true\n"
        "model: gpt-5.4\n"
        "---\n\n"
        "# Deploy\n\n$ARGUMENTS\n",
        encoding="utf-8",
    )
    context = _make_context(tmp_path)
    context.extra_skill_dirs = (extra_root,)

    parsed = lookup_skill_slash_command("/deploy staging", context)
    assert parsed is not None
    command, args = parsed
    result = await command.handler(args, context)

    assert result.submit_prompt is not None
    assert result.submit_model == "gpt-5.4"
    assert "staging" in result.submit_prompt


@pytest.mark.asyncio
async def test_model_command_rejects_values_outside_profile_allowlist(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    save_settings(
        Settings().model_copy(
            update={
                "active_profile": "kimi-anthropic",
                "profiles": {
                    "kimi-anthropic": {
                        "label": "Kimi Anthropic",
                        "provider": "anthropic",
                        "api_format": "anthropic",
                        "auth_source": "anthropic_api_key",
                        "default_model": "kimi-k2.5",
                        "last_model": "kimi-k2.5",
                        "base_url": "https://api.moonshot.cn/anthropic",
                        "allowed_models": ["kimi-k2.5"],
                    }
                },
            }
        )
    )
    registry = create_default_command_registry()
    command, args = registry.lookup("/model claude-opus-4-6")
    assert command is not None

    result = await command.handler(args, CommandContext(engine=_make_engine(tmp_path), cwd=str(tmp_path)))

    assert "is not allowed for profile 'kimi-anthropic'" in result.message


@pytest.mark.asyncio
async def test_doctor_command_reports_context(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    registry = create_default_command_registry()
    command, args = registry.lookup("/doctor")
    assert command is not None

    result = await command.handler(
        args,
        CommandContext(
            engine=_make_engine(tmp_path),
            cwd=str(tmp_path),
            plugin_summary="Plugins:\n- demo [enabled] Example",
            mcp_summary="No MCP servers configured.",
        ),
    )

    assert "Doctor summary:" in result.message
    assert str(tmp_path) in result.message


@pytest.mark.asyncio
async def test_memory_command_manages_entries(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    registry = create_default_command_registry()
    context = _make_context(tmp_path)

    add_command, add_args = registry.lookup("/memory add Pytest Tips :: use fixtures")
    add_result = await add_command.handler(add_args, context)
    assert "Added memory entry" in add_result.message

    list_command, list_args = registry.lookup("/memory list")
    list_result = await list_command.handler(list_args, context)
    assert "pytest_tips.md" in list_result.message

    show_command, show_args = registry.lookup("/memory show pytest_tips")
    show_result = await show_command.handler(show_args, context)
    assert "use fixtures" in show_result.message

    remove_command, remove_args = registry.lookup("/memory remove pytest_tips")
    remove_result = await remove_command.handler(remove_args, context)
    assert "Removed memory entry" in remove_result.message

    list_after_remove = await list_command.handler(list_args, context)
    assert "pytest_tips.md" not in list_after_remove.message

    show_after_remove = await show_command.handler(show_args, context)
    assert show_after_remove.message == "Memory entry not found: pytest_tips"


@pytest.mark.asyncio
async def test_memory_command_migrates_entries(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    memory_dir = get_project_memory_dir(tmp_path)
    legacy = memory_dir / "legacy.md"
    legacy.write_text("legacy command note\n", encoding="utf-8")
    registry = create_default_command_registry()
    context = _make_context(tmp_path)

    dry_command, dry_args = registry.lookup("/memory migrate --dry-run")
    dry_result = await dry_command.handler(dry_args, context)

    assert "Memory migration dry run." in dry_result.message
    assert "Changed: 1" in dry_result.message
    assert "schema_version" not in legacy.read_text(encoding="utf-8")

    apply_command, apply_args = registry.lookup("/memory migrate --apply")
    apply_result = await apply_command.handler(apply_args, context)

    assert "Memory migration applied." in apply_result.message
    assert "Backup:" in apply_result.message
    assert "schema_version: 1" in legacy.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_memory_migrate_uses_backend_defaults_not_label(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    memory_dir = tmp_path / "custom-memory"
    memory_dir.mkdir()
    legacy = memory_dir / "legacy.md"
    legacy.write_text("personal preference note\n", encoding="utf-8")
    registry = create_default_command_registry()
    context = _make_context(tmp_path)
    context.memory_backend = MemoryCommandBackend(
        label="renamed user notes",
        default_type="personal",
        default_category="preference",
        get_memory_dir=lambda: memory_dir,
        get_entrypoint=lambda: memory_dir / "MEMORY.md",
        list_files=list,
        add_entry=lambda title, content: memory_dir / f"{title}.md",
        remove_entry=lambda name: False,
    )

    apply_command, apply_args = registry.lookup("/memory migrate --apply")
    result = await apply_command.handler(apply_args, context)

    migrated = legacy.read_text(encoding="utf-8")
    assert "Memory migration applied." in result.message
    assert 'type: "personal"' in migrated
    assert 'category: "preference"' in migrated


@pytest.mark.asyncio
async def test_compact_summary_and_usage_commands(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    registry = create_default_command_registry()
    context = _make_context(tmp_path)
    context.engine.load_messages(
        [
            ConversationMessage(role="user", content=[TextBlock(text="alpha request")]),
            ConversationMessage(role="assistant", content=[TextBlock(text="alpha reply")]),
            ConversationMessage(role="user", content=[TextBlock(text="beta request")]),
            ConversationMessage(role="assistant", content=[TextBlock(text="beta reply")]),
        ]
    )

    summary_command, summary_args = registry.lookup("/summary 3")
    summary_result = await summary_command.handler(summary_args, context)
    assert "assistant: alpha reply" in summary_result.message or "user: beta request" in summary_result.message

    compact_command, compact_args = registry.lookup("/compact 2")
    compact_result = await compact_command.handler(compact_args, context)
    assert "Compacted conversation" in compact_result.message
    assert len(context.engine.messages) == 3

    usage_command, usage_args = registry.lookup("/usage")
    usage_result = await usage_command.handler(usage_args, context)
    assert "system prompt" in usage_result.message
    assert "tools:" in usage_result.message
    assert "conversation:" in usage_result.message

    stats_command, stats_args = registry.lookup("/stats")
    stats_result = await stats_command.handler(stats_args, context)
    assert "Session stats:" in stats_result.message


@pytest.mark.asyncio
async def test_ui_mode_commands_persist_and_update_state(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    registry = create_default_command_registry()
    context = _make_context(tmp_path)

    config_command, config_args = registry.lookup("/config set verbose true")
    config_result = await config_command.handler(config_args, context)
    assert "Updated verbose" in config_result.message
    assert load_settings().verbose is True

    output_command, output_args = registry.lookup("/output-style set minimal")
    output_result = await output_command.handler(output_args, context)
    assert "minimal" in output_result.message
    assert context.app_state.get().output_style == "minimal"

    keybindings_command, keybindings_args = registry.lookup("/keybindings")
    keybindings_result = await keybindings_command.handler(keybindings_args, context)
    assert "ctrl+l" in keybindings_result.message

    vim_command, vim_args = registry.lookup("/vim toggle")
    vim_result = await vim_command.handler(vim_args, context)
    assert "enabled" in vim_result.message
    assert context.app_state.get().vim_enabled is True

    voice_command, voice_args = registry.lookup("/voice keyterms Shipping pytest fixtures")
    voice_result = await voice_command.handler(voice_args, context)
    assert "pytest" in voice_result.message

    plan_command, plan_args = registry.lookup("/plan on")
    plan_result = await plan_command.handler(plan_args, context)
    assert "enabled" in plan_result.message
    assert load_settings().permission.mode == "plan"

    fast_command, fast_args = registry.lookup("/fast on")
    fast_result = await fast_command.handler(fast_args, context)
    assert "enabled" in fast_result.message
    assert load_settings().fast_mode is True
    assert context.app_state.get().fast_mode is True

    effort_command, effort_args = registry.lookup("/effort high")
    effort_result = await effort_command.handler(effort_args, context)
    assert "high" in effort_result.message
    assert load_settings().effort == "high"
    assert context.app_state.get().effort == "high"

    effort_command, effort_args = registry.lookup("/effort xhigh")
    effort_result = await effort_command.handler(effort_args, context)
    assert "xhigh" in effort_result.message
    assert load_settings().effort == "xhigh"
    assert context.app_state.get().effort == "xhigh"

    effort_command, effort_args = registry.lookup("/effort off")
    effort_result = await effort_command.handler(effort_args, context)
    assert "off" in effort_result.message
    assert load_settings().effort == "off"
    assert context.app_state.get().effort == "off"

    thinking_command, thinking_args = registry.lookup("/thinking on")
    thinking_result = await thinking_command.handler(thinking_args, context)
    assert "enabled" in thinking_result.message
    assert load_settings().effort == "medium"
    assert context.app_state.get().effort == "medium"

    thinking_command, thinking_args = registry.lookup("/thinking off")
    thinking_result = await thinking_command.handler(thinking_args, context)
    assert "disabled" in thinking_result.message
    assert load_settings().effort == "off"

    passes_command, passes_args = registry.lookup("/passes 3")
    passes_result = await passes_command.handler(passes_args, context)
    assert "3" in passes_result.message
    assert load_settings().passes == 3
    assert context.app_state.get().passes == 3


@pytest.mark.asyncio
async def test_notify_command_toggles_and_tests(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    registry = create_default_command_registry()
    context = _make_context(tmp_path)

    show_command, show_args = registry.lookup("/notify show")
    show_result = await show_command.handler(show_args, context)
    assert "Notifications: on" in show_result.message

    off_command, off_args = registry.lookup("/notify off")
    off_result = await off_command.handler(off_args, context)
    assert "disabled" in off_result.message
    assert load_settings().notifications.enabled is False

    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "openharness.notifications.send_notification",
        lambda title, body: sent.append((title, body)) or True,
    )
    test_command, test_args = registry.lookup("/notify test")
    test_result = await test_command.handler(test_args, context)
    assert "sent" in test_result.message
    assert sent == [("Shim", "Test notification from Shim.")]

    on_command, on_args = registry.lookup("/notify on")
    on_result = await on_command.handler(on_args, context)
    assert "enabled" in on_result.message
    assert load_settings().notifications.enabled is True


@pytest.mark.asyncio
async def test_loop_command_creates_lists_and_stops_jobs(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    registry = create_default_command_registry()
    context = _make_context(tmp_path)

    created: list[dict] = []
    deleted: list[str] = []
    stored: dict[str, dict] = {}

    def fake_upsert(job: dict) -> None:
        created.append(dict(job))
        stored[str(job["name"])] = dict(job, next_run="2026-11-16T12:00:00+00:00")

    monkeypatch.setattr("openharness.services.cron.upsert_cron_job", fake_upsert)
    monkeypatch.setattr(
        "openharness.services.cron.load_cron_jobs",
        lambda: list(stored.values()),
    )
    monkeypatch.setattr(
        "openharness.services.cron.get_cron_job",
        lambda name: stored.get(name),
    )

    def fake_delete(name: str) -> bool:
        deleted.append(name)
        stored.pop(name, None)
        return True

    monkeypatch.setattr("openharness.services.cron.delete_cron_job", fake_delete)
    monkeypatch.setattr(
        "openharness.services.cron_scheduler.is_scheduler_running",
        lambda: True,
    )

    loop_command, loop_args = registry.lookup("/loop 5m check the build")
    loop_result = await loop_command.handler(loop_args, context)

    assert "Loop created: loop-" in loop_result.message
    assert "*/5 * * * *" in loop_result.message
    assert created
    job = created[0]
    assert job["loop"] is True
    assert job["schedule"] == "*/5 * * * *"
    assert job["payload"]["kind"] == "shim_turn"
    assert job["payload"]["message"] == "check the build"
    assert job["payload"]["model"] == context.engine.model

    list_command, list_args = registry.lookup("/loop list")
    list_result = await list_command.handler(list_args, context)
    assert job["name"] in list_result.message
    assert "check the build" in list_result.message

    stop_command, stop_args = registry.lookup("/loop stop all")
    stop_result = await stop_command.handler(stop_args, context)
    assert "Stopped loop job" in stop_result.message
    assert job["name"] in deleted

    invalid_command, invalid_args = registry.lookup("/loop 30s too fast")
    invalid_result = await invalid_command.handler(invalid_args, context)
    assert "minimum loop interval" in invalid_result.message


@pytest.mark.asyncio
async def test_tools_command_switches_profile_and_enables_deferred_tool(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    registry = create_default_command_registry()
    context = _make_context(tmp_path)

    show_command, show_args = registry.lookup("/tools show")
    show_result = await show_command.handler(show_args, context)
    assert "Tool profile: core" in show_result.message
    assert "image_generation" in show_result.message

    enable_command, enable_args = registry.lookup("/tools enable image_generation")
    enable_result = await enable_command.handler(enable_args, context)
    assert "image_generation" in enable_result.message
    assert context.engine.visible_tool_names is not None
    assert "image_generation" in context.engine.visible_tool_names

    full_command, full_args = registry.lookup("/tools profile full")
    full_result = await full_command.handler(full_args, context)
    assert "full" in full_result.message
    assert load_settings().tool_profile == "full"
    assert context.engine.visible_tool_names is None

    core_command, core_args = registry.lookup("/tools profile core")
    core_result = await core_command.handler(core_args, context)
    assert "core" in core_result.message
    assert load_settings().tool_profile == "core"


@pytest.mark.asyncio
async def test_personality_commands_switch_and_customize_prompt(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    registry = create_default_command_registry()
    context = _make_context(tmp_path)

    show_command, show_args = registry.lookup("/personality show")
    show_result = await show_command.handler(show_args, context)
    assert "Personality: shim" in show_result.message

    list_command, list_args = registry.lookup("/personality list")
    list_result = await list_command.handler(list_args, context)
    assert "concise" in list_result.message
    assert "off" in list_result.message

    set_command, set_args = registry.lookup("/personality set concise")
    set_result = await set_command.handler(set_args, context)
    assert "concise" in set_result.message
    assert load_settings().personality == "concise"
    assert context.app_state.get().personality == "concise"
    assert "# Persona: Concise" in context.engine.system_prompt

    custom_command, custom_args = registry.lookup(
        "/personality custom Answer only in haiku."
    )
    custom_result = await custom_command.handler(custom_args, context)
    assert "custom" in custom_result.message
    assert load_settings().personality == "custom"
    assert "Answer only in haiku." in context.engine.system_prompt

    off_command, off_args = registry.lookup("/personality off")
    off_result = await off_command.handler(off_args, context)
    assert "disabled" in off_result.message
    assert load_settings().personality == "off"
    assert "# Persona:" not in context.engine.system_prompt


@pytest.mark.asyncio
async def test_thinking_show_and_probe_report_real_evidence(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    registry = create_default_command_registry()
    context = _make_context(tmp_path)

    show_command, show_args = registry.lookup("/thinking show")
    show_result = await show_command.handler(show_args, context)
    assert "No assistant turn yet" in show_result.message

    context.engine.load_messages(
        [
            ConversationMessage.from_user_text("hi"),
            ConversationMessage(
                role="assistant",
                content=[
                    ThinkingBlock(thinking="abc", signature="sig"),
                    TextBlock(text="ok"),
                ],
            ),
        ]
    )
    show_result = await show_command.handler(show_args, context)
    assert "1 thinking block" in show_result.message
    assert "3 chars" in show_result.message
    assert "Last thinking:\nabc" in show_result.message

    full_command, full_args = registry.lookup("/thinking show full")
    full_result = await full_command.handler(full_args, context)
    assert "Last thinking:\nabc" in full_result.message

    class _ProbeClient:
        async def stream_message(self, request):
            del request
            yield ApiMessageCompleteEvent(
                message=ConversationMessage(
                    role="assistant",
                    content=[
                        ThinkingBlock(thinking="probe", signature="sig"),
                        TextBlock(text="3293"),
                    ],
                ),
                usage=UsageSnapshot(input_tokens=10, output_tokens=5),
            )

    context.engine._api_client = _ProbeClient()  # type: ignore[attr-defined]
    probe_command, probe_args = registry.lookup("/thinking probe")
    probe_result = await probe_command.handler(probe_args, context)
    assert "thinking is active" in probe_result.message
    assert "output tokens: 5" in probe_result.message
    assert "effort=medium" in probe_result.message


@pytest.mark.asyncio
async def test_version_context_and_share_commands(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    registry = create_default_command_registry()
    context = _make_context(tmp_path)

    version_command, version_args = registry.lookup("/version")
    version_result = await version_command.handler(version_args, context)
    assert version_result.message == f"OpenHarness {__version__}"

    context_command, context_args = registry.lookup("/context")
    context_result = await context_command.handler(context_args, context)
    assert context_result.message == context.engine.system_prompt

    share_command, share_args = registry.lookup("/share")
    share_result = await share_command.handler(share_args, context)
    assert "shareable transcript snapshot" in share_result.message


@pytest.mark.asyncio
async def test_auth_feedback_and_project_context_commands(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    # Prevent env var leakage from overriding the configured api_key
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    registry = create_default_command_registry()
    context = _make_context(tmp_path)

    login_command, login_args = registry.lookup("/login sk-test-123456")
    login_result = await login_command.handler(login_args, context)
    assert "Stored API key" in login_result.message
    assert load_settings().api_key == "sk-test-123456"

    issue_command, issue_args = registry.lookup("/issue set Fix CI :: The CI flakes on task retry")
    issue_result = await issue_command.handler(issue_args, context)
    assert "Saved issue context" in issue_result.message
    assert "Fix CI" in get_project_issue_file(tmp_path).read_text(encoding="utf-8")

    pr_command, pr_args = registry.lookup("/pr_comments add src/app.py:12 :: simplify this branch")
    pr_result = await pr_command.handler(pr_args, context)
    assert "Added PR comment" in pr_result.message
    assert "simplify this branch" in get_project_pr_comments_file(tmp_path).read_text(encoding="utf-8")

    feedback_command, feedback_args = registry.lookup("/feedback this workflow feels good")
    feedback_result = await feedback_command.handler(feedback_args, context)
    assert "Saved feedback" in feedback_result.message
    assert "this workflow feels good" in get_feedback_log_path().read_text(encoding="utf-8")

    logout_command, logout_args = registry.lookup("/logout")
    logout_result = await logout_command.handler(logout_args, context)
    assert "Cleared stored API key" in logout_result.message
    assert load_settings().api_key == ""


@pytest.mark.asyncio
async def test_agents_session_files_and_reload_plugins_commands(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    registry = create_default_command_registry()
    context = _make_context(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")

    session_command, session_args = registry.lookup("/session")
    context.session_id = "session-smoke"
    session_result = await session_command.handler(session_args, context)
    assert "Session ID: session-smoke" in session_result.message
    assert "Session directory:" in session_result.message

    session_path_command, session_path_args = registry.lookup("/session path")
    session_path_result = await session_path_command.handler(session_path_args, context)
    assert "sessions" in session_path_result.message

    session_tag_command, session_tag_args = registry.lookup("/session tag smoke")
    session_tag_result = await session_tag_command.handler(session_tag_args, context)
    assert "smoke.json" in session_tag_result.message
    assert "smoke.md" in session_tag_result.message

    tag_command, tag_args = registry.lookup("/tag alias-smoke")
    tag_result = await tag_command.handler(tag_args, context)
    assert "alias-smoke.json" in tag_result.message
    assert "alias-smoke.md" in tag_result.message

    files_command, files_args = registry.lookup("/files app.py")
    files_result = await files_command.handler(files_args, context)
    assert "src/app.py" in files_result.message.replace("\\", "/")

    files_dirs_command, files_dirs_args = registry.lookup("/files dirs")
    files_dirs_result = await files_dirs_command.handler(files_dirs_args, context)
    assert "src" in files_dirs_result.message

    plugin_root = tmp_path / "config" / "plugins" / "fixture-plugin"
    (plugin_root / "skills").mkdir(parents=True)
    (plugin_root / "plugin.json").write_text(
        '{"name":"fixture-plugin","version":"1.0.0","description":"Fixture plugin"}',
        encoding="utf-8",
    )
    reload_command, reload_args = registry.lookup("/reload-plugins")
    reload_result = await reload_command.handler(reload_args, context)
    assert "fixture-plugin" in reload_result.message

    manager = get_task_manager()
    task = await manager.create_agent_task(
        prompt="ready",
        description="test agent",
        cwd=tmp_path,
        command=(
            f'"{sys.executable}" -u -c "import sys; print(sys.stdin.readline().strip())"'
        ),
    )
    agents_command, agents_args = registry.lookup("/agents")
    agents_result = await agents_command.handler(agents_args, context)
    assert task.id in agents_result.message

    agent_show_command, agent_show_args = registry.lookup(f"/agents show {task.id}")
    agent_show_result = await agent_show_command.handler(agent_show_args, context)
    assert "test agent" in agent_show_result.message


@pytest.mark.asyncio
async def test_agents_help_and_subagents_alias(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    registry = create_default_command_registry()
    context = _make_context(tmp_path)

    agents_help_command, agents_help_args = registry.lookup("/agents help")
    assert agents_help_command is not None
    agents_help_result = await agents_help_command.handler(agents_help_args, context)
    assert "Subagent guide:" in agents_help_result.message
    assert 'subagent_type="worker"' in agents_help_result.message

    subagents_command, subagents_args = registry.lookup("/subagents")
    assert subagents_command is not None
    subagents_result = await subagents_command.handler(subagents_args, context)
    assert "Subagent guide:" in subagents_result.message

    agents_command, agents_args = registry.lookup("/agents")
    assert agents_command is not None
    agents_result = await agents_command.handler(agents_args, context)
    assert "Subagent guide:" in agents_result.message


@pytest.mark.asyncio
async def test_agents_follow_and_unfollow(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    registry = create_default_command_registry()
    context = _make_context(tmp_path)
    task = SimpleNamespace(
        id="a1f2e3d4",
        type="local_agent",
        status="running",
        description="worker",
    )

    class _Manager:
        def get_task(self, task_id: str):
            return task if task_id == task.id else None

        def list_tasks(self, status=None):
            del status
            return [task]

    monkeypatch.setattr(
        "openharness.commands.builtin.workspace.get_task_manager",
        lambda: _Manager(),
    )
    started: list[tuple[str, object]] = []
    stopped: list[str | None] = []

    def fake_start(task_id: str, emit) -> None:
        started.append((task_id, emit))

    async def fake_stop(target: str | None) -> list[str]:
        stopped.append(target)
        return [task.id]

    monkeypatch.setattr(
        "openharness.commands.builtin.workspace.start_task_follow", fake_start
    )
    monkeypatch.setattr(
        "openharness.commands.builtin.workspace.stop_task_follow", fake_stop
    )
    monkeypatch.setattr(
        "openharness.commands.builtin.workspace.active_task_follows",
        lambda: [task.id],
    )

    follow_command, follow_args = registry.lookup("/agents follow")
    no_printer = await follow_command.handler(follow_args, context)
    assert "interactive transcript" in no_printer.message

    async def printer(line: str) -> None:
        del line

    context.print_system = printer
    follow_result = await follow_command.handler(follow_args, context)
    assert follow_result.message is not None
    assert f"Following {task.id}" in follow_result.message
    assert started == [(task.id, printer)]

    list_command, list_args = registry.lookup("/agents")
    listing = await list_command.handler(list_args, context)
    assert "(following)" in listing.message

    unfollow_command, unfollow_args = registry.lookup("/agents unfollow")
    unfollow_result = await unfollow_command.handler(unfollow_args, context)
    assert unfollow_result.message is not None
    assert f"Stopped following: {task.id}" in unfollow_result.message
    assert stopped == [None]


@pytest.mark.asyncio
async def test_init_and_bridge_commands(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    registry = create_default_command_registry()
    context = _make_context(tmp_path)

    init_command, init_args = registry.lookup("/init")
    init_result = await init_command.handler(init_args, context)
    assert "Initialized project files" in init_result.message or "already initialized" in init_result.message
    assert (tmp_path / "CLAUDE.md").exists()
    assert (tmp_path / ".openharness" / "memory" / "MEMORY.md").exists()

    bridge_show_command, bridge_show_args = registry.lookup("/bridge show")
    bridge_show_result = await bridge_show_command.handler(bridge_show_args, context)
    assert "Bridge summary:" in bridge_show_result.message

    bridge_encode_command, bridge_encode_args = registry.lookup("/bridge encode https://api.example.com token123")
    bridge_encode_result = await bridge_encode_command.handler(bridge_encode_args, context)
    assert bridge_encode_result.message

    bridge_decode_command, bridge_decode_args = registry.lookup(f"/bridge decode {bridge_encode_result.message}")
    bridge_decode_result = await bridge_decode_command.handler(bridge_decode_args, context)
    assert "api.example.com" in bridge_decode_result.message

    bridge_sdk_command, bridge_sdk_args = registry.lookup("/bridge sdk https://api.example.com session123")
    bridge_sdk_result = await bridge_sdk_command.handler(bridge_sdk_args, context)
    assert "session123" in bridge_sdk_result.message

    bridge_spawn_command, bridge_spawn_args = registry.lookup("/bridge spawn printf bridge-ok")
    bridge_spawn_result = await bridge_spawn_command.handler(bridge_spawn_args, context)
    assert "Spawned bridge session" in bridge_spawn_result.message
    session_id = bridge_spawn_result.message.split()[3]

    bridge_list_command, bridge_list_args = registry.lookup("/bridge list")
    bridge_list_result = await bridge_list_command.handler(bridge_list_args, context)
    assert session_id in bridge_list_result.message

    bridge_output_command, bridge_output_args = registry.lookup(f"/bridge output {session_id}")
    bridge_output_result = await bridge_output_command.handler(bridge_output_args, context)
    assert "bridge-ok" in bridge_output_result.message or bridge_output_result.message == "(no output)"

    bridge_stop_command, bridge_stop_args = registry.lookup(f"/bridge stop {session_id}")
    bridge_stop_result = await bridge_stop_command.handler(bridge_stop_args, context)
    assert f"Stopped bridge session {session_id}" in bridge_stop_result.message


@pytest.mark.asyncio
async def test_copy_rewind_and_meta_commands(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    registry = create_default_command_registry()
    context = _make_context(tmp_path)
    context.engine.load_messages(
        [
            ConversationMessage.from_user_text("first prompt"),
            ConversationMessage(role="assistant", content=[TextBlock(text="first answer")]),
            ConversationMessage.from_user_text("second prompt"),
            ConversationMessage(role="assistant", content=[TextBlock(text="second answer")]),
        ]
    )

    copied: list[str] = []

    def _fake_copy(text: str) -> None:
        copied.append(text)

    monkeypatch.setattr("openharness.commands.helpers.pyperclip.copy", _fake_copy)

    copy_command, copy_args = registry.lookup("/copy")
    copy_result = await copy_command.handler(copy_args, context)
    assert "Copied" in copy_result.message
    assert copied == ["second answer"]

    rewind_command, rewind_args = registry.lookup("/rewind 1")
    rewind_result = await rewind_command.handler(rewind_args, context)
    assert "removed 2 message(s)" in rewind_result.message
    assert len(context.engine.messages) == 2

    privacy_command, privacy_args = registry.lookup("/privacy-settings")
    privacy_result = await privacy_command.handler(privacy_args, context)
    assert "user_config_dir" in privacy_result.message

    rate_command, rate_args = registry.lookup("/rate-limit-options")
    rate_result = await rate_command.handler(rate_args, context)
    assert "Rate limit options:" in rate_result.message

    release_command, release_args = registry.lookup("/release-notes")
    release_result = await release_command.handler(release_args, context)
    assert "Release Notes" in release_result.message

    upgrade_command, upgrade_args = registry.lookup("/upgrade")
    upgrade_result = await upgrade_command.handler(upgrade_args, context)
    assert f"Current version: {__version__}" in upgrade_result.message
    assert "Upgrade instructions:" in upgrade_result.message


@pytest.mark.asyncio
async def test_mcp_and_voice_commands_report_richer_state(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    settings = Settings(
        mcp_servers={
            "http-demo": McpHttpServerConfig(url="https://example.com/mcp"),
            "stdio-demo": McpStdioServerConfig(command="python", args=["-m", "demo"]),
        }
    )
    save_settings(settings)

    registry = create_default_command_registry()
    context = _make_context(tmp_path)

    mcp_http_command, mcp_http_args = registry.lookup("/mcp auth http-demo secret-token")
    mcp_http_result = await mcp_http_command.handler(mcp_http_args, context)
    assert "Saved MCP auth for http-demo" in mcp_http_result.message
    assert load_settings().mcp_servers["http-demo"].headers["Authorization"] == "Bearer secret-token"

    mcp_stdio_command, mcp_stdio_args = registry.lookup("/mcp auth stdio-demo env DEMO_TOKEN")
    mcp_stdio_result = await mcp_stdio_command.handler(mcp_stdio_args, context)
    assert "Saved MCP auth for stdio-demo" in mcp_stdio_result.message
    assert load_settings().mcp_servers["stdio-demo"].env["MCP_AUTH_TOKEN"] == "DEMO_TOKEN"

    voice_command, voice_args = registry.lookup("/voice show")
    voice_result = await voice_command.handler(voice_args, context)
    assert "Voice mode:" in voice_result.message
    assert "Available:" in voice_result.message
    assert "Reason:" in voice_result.message


@pytest.mark.asyncio
async def test_git_commands_report_repository_state(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    _init_git_repo(tmp_path)
    (tmp_path / "demo.txt").write_text("hello\n", encoding="utf-8")

    registry = create_default_command_registry()
    context = _make_context(tmp_path)

    branch_command, branch_args = registry.lookup("/branch show")
    branch_result = await branch_command.handler(branch_args, context)
    assert "Current branch" in branch_result.message

    diff_command, diff_args = registry.lookup("/diff")
    diff_result = await diff_command.handler(diff_args, context)
    assert "demo.txt" in diff_result.message or "(no diff)" in diff_result.message

    commit_command, commit_args = registry.lookup("/commit initial commit")
    commit_result = await commit_command.handler(commit_args, context)
    assert "commit" in commit_result.message.lower()


def test_quit_is_alias_for_exit():
    """Regression for #183: /quit should resolve to the same handler as /exit."""
    reg = create_default_command_registry()

    exit_cmd, _ = reg.lookup("/exit")
    quit_cmd, _ = reg.lookup("/quit")

    assert exit_cmd is quit_cmd
    assert quit_cmd.name == "exit"


def test_help_and_list_do_not_duplicate_aliases():
    """Aliases share a SlashCommand object; help/listing must not repeat it."""
    reg = create_default_command_registry()

    names = [cmd.name for cmd in reg.list_commands()]
    assert names.count("exit") == 1
    assert "quit" not in names  # alias is resolvable via lookup, not listed

    help_text = reg.help_text()
    assert help_text.count("/exit ") == 1
    assert "/quit" not in help_text


@pytest.mark.asyncio
async def test_slash_commands_clear_startup_overrides(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    registry = create_default_command_registry()
    context = _make_context(tmp_path)
    context.settings_overrides = {
        "personality": "shim",
        "effort": "low",
        "tool_profile": "full",
        "permission_mode": "plan",
        "max_turns": 8,
    }

    for line in (
        "/personality set concise",
        "/effort high",
        "/tools profile core",
        "/permissions default",
        "/turns 12",
    ):
        command, args = registry.lookup(line)
        await command.handler(args, context)

    assert context.settings_overrides == {}


@pytest.mark.asyncio
async def test_resume_keeps_a_persisted_plan_file(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    plan_file = tmp_path / "sessions" / "plan-prev123.md"
    snapshot = {
        "session_id": "prev123",
        "messages": [
            ConversationMessage(role="user", content=[TextBlock(text="hello")]).model_dump(mode="json"),
        ],
        "tool_metadata": {"plan_file": str(plan_file)},
    }
    registry = create_default_command_registry()
    command, args = registry.lookup("/resume prev123")
    assert command is not None

    engine = _make_engine(tmp_path)
    engine.tool_metadata["plan_file"] = str(tmp_path / "sessions" / "plan-current.md")
    context = CommandContext(
        engine=engine,
        cwd=str(tmp_path),
        session_backend=_FakeResumeSessionBackend(snapshot),  # type: ignore[arg-type]
    )

    await command.handler(args, context)

    assert engine.tool_metadata["plan_file"] == str(plan_file)


class _FakeResumeSessionBackend:
    """Session backend that returns a single prepared snapshot."""

    def __init__(self, snapshot: dict) -> None:
        self._snapshot = snapshot

    def load_by_id(self, cwd: str, session_id: str) -> dict:
        del cwd, session_id
        return self._snapshot

    def list_snapshots(self, cwd: str, limit: int = 20) -> list[dict]:
        del cwd, limit
        return []


@pytest.mark.asyncio
async def test_resume_restores_work_state_metadata(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    snapshot = {
        "session_id": "prev123",
        "summary": "earlier work",
        "messages": [
            ConversationMessage(role="user", content=[TextBlock(text="hello")]).model_dump(mode="json"),
        ],
        "tool_metadata": {
            "read_file_state": [{"path": "src/app.py"}],
            "recent_work_log": ["edited src/app.py"],
            "recent_verified_work": ["pytest tests/test_app.py"],
            "task_focus_state": {"goal": "ship the fix"},
            "compact_checkpoints": [],
            # Live mode wins: a stale snapshot must not override it.
            "permission_mode": "plan",
        },
    }
    registry = create_default_command_registry()
    command, args = registry.lookup("/resume prev123")
    assert command is not None

    engine = _make_engine(tmp_path)
    context = CommandContext(
        engine=engine,
        cwd=str(tmp_path),
        session_backend=_FakeResumeSessionBackend(snapshot),  # type: ignore[arg-type]
    )

    result = await command.handler(args, context)

    assert "Restored 1 messages" in result.message
    assert engine.tool_metadata["read_file_state"] == [{"path": "src/app.py"}]
    assert engine.tool_metadata["recent_work_log"] == ["edited src/app.py"]
    assert engine.tool_metadata["recent_verified_work"] == ["pytest tests/test_app.py"]
    assert engine.tool_metadata["task_focus_state"] == {"goal": "ship the fix"}
    assert "permission_mode" not in engine.tool_metadata
    # Snapshots without a persisted plan file must not keep pointing at the
    # plan file of the session we just switched away from.
    assert engine.tool_metadata["plan_file"] == str(
        get_project_session_dir(tmp_path) / "plan-prev123.md"
    )
