import json
import logging
from pathlib import Path

from typer.testing import CliRunner

from ohmo.cli import _build_gateway_logging_handlers, app


def test_ohmo_help():
    runner = CliRunner()
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "personal-agent app" in result.output
    assert "config" in result.output


def test_ohmo_init_and_doctor(tmp_path: Path):
    runner = CliRunner()
    workspace = tmp_path / ".ohmo-home"
    result = runner.invoke(app, ["init", "--workspace", str(workspace), "--no-interactive"])
    assert result.exit_code == 0
    assert str(workspace) in result.output

    doctor = runner.invoke(app, ["doctor", "--cwd", str(tmp_path), "--workspace", str(workspace)])
    assert doctor.exit_code == 0
    assert "ohmo doctor:" in doctor.output
    assert "workspace: ok" in doctor.output


def test_ohmo_init_existing_workspace_points_to_config(tmp_path: Path):
    runner = CliRunner()
    workspace = tmp_path / ".ohmo-home"
    first = runner.invoke(app, ["init", "--workspace", str(workspace), "--no-interactive"])
    assert first.exit_code == 0

    second = runner.invoke(app, ["init", "--workspace", str(workspace), "--no-interactive"])
    assert second.exit_code == 0
    assert "ohmo workspace already exists." in second.output
    assert "Use `ohmo config`" in second.output


def test_ohmo_init_noninteractive_defaults_to_deny_all_remote_access(tmp_path: Path):
    runner = CliRunner()
    workspace = tmp_path / ".ohmo-home"
    result = runner.invoke(app, ["init", "--workspace", str(workspace), "--no-interactive"])
    assert result.exit_code == 0
    config = json.loads((workspace / "gateway.json").read_text(encoding="utf-8"))
    assert config["channel_configs"] == {}


def test_gateway_logging_handlers_write_gateway_log_file(tmp_path: Path):
    workspace = tmp_path / ".ohmo-home"
    runner = CliRunner()
    result = runner.invoke(app, ["init", "--workspace", str(workspace), "--no-interactive"])
    assert result.exit_code == 0

    handlers = _build_gateway_logging_handlers(workspace, console=True, log_file=True)
    try:
        file_handlers = [handler for handler in handlers if isinstance(handler, logging.FileHandler)]
        console_handlers = [
            handler
            for handler in handlers
            if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler)
        ]
        assert len(file_handlers) == 1
        assert len(console_handlers) == 1

        record = logging.LogRecord(
            name="ohmo.gateway.test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="GATEWAY_LOG_OK",
            args=(),
            exc_info=None,
        )
        file_handlers[0].emit(record)
        file_handlers[0].flush()

        log_path = workspace / "logs" / "gateway.log"
        assert "GATEWAY_LOG_OK" in log_path.read_text(encoding="utf-8")
    finally:
        for handler in handlers:
            handler.close()


def test_ohmo_init_interactive_writes_gateway_config(tmp_path: Path, monkeypatch):
    runner = CliRunner()
    workspace = tmp_path / ".ohmo-home"
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    user_input = "\n".join(
        [
            "1",  # provider profile
            "y",  # enable telegram
            "123456",  # allow_from
            "telegram-token",
            "y",  # reply_to_message
            "n",  # slack
            "n",  # discord
            "n",  # feishu
            "y",  # send_progress
            "y",  # send_tool_hints
            "n",  # allow_remote_admin_commands
        ]
    )
    result = runner.invoke(app, ["init", "--workspace", str(workspace)], input=user_input)
    assert result.exit_code == 0
    config = json.loads((workspace / "gateway.json").read_text(encoding="utf-8"))
    assert config["enabled_channels"] == ["telegram"]
    assert config["channel_configs"]["telegram"]["token"] == "telegram-token"
    assert config["channel_configs"]["telegram"]["allow_from"] == ["123456"]


def test_ohmo_init_interactive_allows_blank_allow_from_for_secure_default(tmp_path: Path, monkeypatch):
    runner = CliRunner()
    workspace = tmp_path / ".ohmo-home"
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    user_input = "\n".join(
        [
            "1",  # provider profile
            "y",  # enable telegram
            "",   # allow_from -> deny all until explicitly configured
            "telegram-token",
            "y",  # reply_to_message
            "n",  # slack
            "n",  # discord
            "n",  # feishu
            "y",  # send_progress
            "y",  # send_tool_hints
            "n",  # allow_remote_admin_commands
        ]
    )
    result = runner.invoke(app, ["init", "--workspace", str(workspace)], input=user_input)
    assert result.exit_code == 0
    config = json.loads((workspace / "gateway.json").read_text(encoding="utf-8"))
    assert config["channel_configs"]["telegram"]["allow_from"] == []
    assert "Remote access denied until allow_from is configured for: telegram" in result.output


def test_ohmo_init_interactive_writes_feishu_gateway_config(tmp_path: Path, monkeypatch):
    runner = CliRunner()
    workspace = tmp_path / ".ohmo-home"
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    user_input = "\n".join(
        [
            "1",         # provider profile
            "n",         # telegram
            "n",         # slack
            "n",         # discord
            "y",         # feishu
            "feishu-user-1",         # allow_from
            "1",         # domain -> Feishu (China)
            "cli_app",   # app_id
            "cli_secret",# app_secret
            "enc_key",   # encrypt_key
            "verify_me", # verification_token
            "OK",        # react_emoji
            "1",         # group_policy -> managed_or_mention
            "ohmo,openclaw", # bot_names
            "",          # bot_open_id
            "y",         # send_progress
            "n",         # send_tool_hints
            "n",         # allow_remote_admin_commands
        ]
    )
    result = runner.invoke(app, ["init", "--workspace", str(workspace)], input=user_input)
    assert result.exit_code == 0
    config = json.loads((workspace / "gateway.json").read_text(encoding="utf-8"))
    assert config["enabled_channels"] == ["feishu"]
    assert config["channel_configs"]["feishu"]["domain"] == "https://open.feishu.cn"
    assert config["channel_configs"]["feishu"]["app_id"] == "cli_app"
    assert config["channel_configs"]["feishu"]["app_secret"] == "cli_secret"
    assert config["channel_configs"]["feishu"]["encrypt_key"] == "enc_key"
    assert config["channel_configs"]["feishu"]["verification_token"] == "verify_me"
    assert config["channel_configs"]["feishu"]["react_emoji"] == "OK"
    assert config["channel_configs"]["feishu"]["group_policy"] == "managed_or_mention"
    assert config["channel_configs"]["feishu"]["bot_names"] == ["ohmo", "openclaw"]


def test_ohmo_config_interactive_can_restart_gateway(tmp_path: Path, monkeypatch):
    runner = CliRunner()
    workspace = tmp_path / ".ohmo-home"
    runner.invoke(app, ["init", "--workspace", str(workspace), "--no-interactive"])
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("ohmo.cli.gateway_status", lambda cwd, workspace: type("State", (), {"running": True})())
    monkeypatch.setattr("ohmo.cli.stop_gateway_process", lambda cwd, workspace: True)
    monkeypatch.setattr("ohmo.cli.start_gateway_process", lambda cwd, workspace: 4321)
    user_input = "\n".join(
        [
            "4",          # provider profile -> codex
            "n",          # telegram
            "n",          # slack
            "n",          # discord
            "y",          # feishu
            "feishu-user-1",          # allow_from
            "2",          # domain -> Lark (International)
            "cli_app",    # app_id
            "cli_secret", # app_secret
            "",           # encrypt_key
            "verify_me",  # verification_token
            "OK",         # react_emoji
            "1",          # group_policy -> managed_or_mention
            "ohmo,openclaw", # bot_names
            "",           # bot_open_id
            "y",          # send_progress
            "y",          # send_tool_hints
            "n",          # allow_remote_admin_commands
            "y",          # restart gateway
        ]
    )
    result = runner.invoke(app, ["config", "--workspace", str(workspace)], input=user_input)
    assert result.exit_code == 0
    assert "ohmo gateway restarted (pid=4321)" in result.output
    config = json.loads((workspace / "gateway.json").read_text(encoding="utf-8"))
    assert config["provider_profile"] == "codex"
    assert config["enabled_channels"] == ["feishu"]
    assert config["channel_configs"]["feishu"]["domain"] == "https://open.larksuite.com"


def test_ohmo_config_keeps_existing_channel_when_not_reconfigured(tmp_path: Path, monkeypatch):
    runner = CliRunner()
    workspace = tmp_path / ".ohmo-home"
    runner.invoke(app, ["init", "--workspace", str(workspace), "--no-interactive"])
    gateway_path = workspace / "gateway.json"
    config = json.loads(gateway_path.read_text(encoding="utf-8"))
    config["enabled_channels"] = ["feishu"]
    config["channel_configs"]["feishu"] = {
        "allow_from": ["feishu-user-1"],
        "app_id": "old_app",
        "app_secret": "old_secret",
        "encrypt_key": "",
        "verification_token": "old_verify",
        "react_emoji": "OK",
    }
    gateway_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("ohmo.cli.gateway_status", lambda cwd, workspace: type("State", (), {"running": False})())
    user_input = "\n".join(
        [
            "4",  # provider profile -> codex
            "n",  # telegram
            "n",  # slack
            "n",  # discord
            "n",  # reconfigure feishu? keep existing
            "y",  # send_progress
            "y",  # send_tool_hints
            "n",  # allow_remote_admin_commands
        ]
    )
    result = runner.invoke(app, ["config", "--workspace", str(workspace)], input=user_input)
    assert result.exit_code == 0
    updated = json.loads(gateway_path.read_text(encoding="utf-8"))
    assert updated["enabled_channels"] == ["feishu"]
    assert updated["channel_configs"]["feishu"]["app_id"] == "old_app"
    assert updated["channel_configs"]["feishu"]["app_secret"] == "old_secret"


def test_ohmo_config_preserves_channels_outside_wizard(tmp_path: Path, monkeypatch):
    runner = CliRunner()
    workspace = tmp_path / ".ohmo-home"
    runner.invoke(app, ["init", "--workspace", str(workspace), "--no-interactive"])
    gateway_path = workspace / "gateway.json"
    config = json.loads(gateway_path.read_text(encoding="utf-8"))
    config["enabled_channels"] = ["qq"]
    config["channel_configs"]["qq"] = {
        "allow_from": ["USER-OPENID"],
        "app_id": "102000",
        "app_secret": "qq-secret",
    }
    gateway_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("ohmo.cli.gateway_status", lambda cwd, workspace: type("State", (), {"running": False})())
    user_input = "\n".join(
        [
            "4",  # provider profile -> codex
            "n",  # telegram
            "n",  # slack
            "n",  # discord
            "n",  # feishu
            "y",  # send_progress
            "y",  # send_tool_hints
            "n",  # allow_remote_admin_commands
        ]
    )
    result = runner.invoke(app, ["config", "--workspace", str(workspace)], input=user_input)
    assert result.exit_code == 0
    updated = json.loads(gateway_path.read_text(encoding="utf-8"))
    assert updated["enabled_channels"] == ["qq"]
    assert updated["channel_configs"]["qq"]["app_secret"] == "qq-secret"


def test_backend_command_forwards_continue_and_resume():
    from ohmo.runtime import build_ohmo_backend_command

    command = build_ohmo_backend_command(cwd="C:/work", workspace="C:/ohmo", continue_session=True)
    assert command[-1] == "--continue"
    assert "--resume" not in command

    command = build_ohmo_backend_command(cwd="C:/work", workspace="C:/ohmo", resume="abc123")
    assert command[-2:] == ["--resume", "abc123"]

    command = build_ohmo_backend_command(cwd="C:/work", workspace="C:/ohmo")
    assert "--continue" not in command
    assert "--resume" not in command


def test_ohmo_continue_forwards_to_tui_backend(tmp_path: Path, monkeypatch):
    runner = CliRunner()
    workspace = tmp_path / ".ohmo-home"
    assert runner.invoke(app, ["init", "--workspace", str(workspace), "--no-interactive"]).exit_code == 0
    session_dir = workspace / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "latest.json").write_text(
        json.dumps({"session_id": "prev123", "messages": [], "tool_metadata": {}}),
        encoding="utf-8",
    )

    captured: dict[str, object] = {}

    async def _fake_launch(**kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr("ohmo.cli.launch_ohmo_react_tui", _fake_launch)

    result = runner.invoke(app, ["--continue", "--workspace", str(workspace), "--cwd", str(tmp_path)])

    assert result.exit_code == 0
    assert captured["continue_session"] is True
    assert captured["resume"] is None


def test_ohmo_continue_without_saved_session_fails_fast(tmp_path: Path, monkeypatch):
    runner = CliRunner()
    workspace = tmp_path / ".ohmo-home"
    assert runner.invoke(app, ["init", "--workspace", str(workspace), "--no-interactive"]).exit_code == 0

    async def _fail_launch(**kwargs: object) -> int:
        raise AssertionError("TUI must not launch when no session can be restored")

    monkeypatch.setattr("ohmo.cli.launch_ohmo_react_tui", _fail_launch)

    result = runner.invoke(app, ["--continue", "--workspace", str(workspace), "--cwd", str(tmp_path)])

    assert result.exit_code == 1
    assert "No previous ohmo session found" in (result.output + (result.stderr or ""))


def test_gateway_logs_prints_tail(tmp_path: Path):
    workspace = tmp_path / ".ohmo-home"
    log_dir = workspace / "logs"
    log_dir.mkdir(parents=True)
    log_path = log_dir / "gateway.log"
    log_path.write_text("one\ntwo\nthree\n", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(app, ["gateway", "logs", "--workspace", str(workspace), "--lines", "2"])
    assert result.exit_code == 0
    assert "two" in result.output
    assert "three" in result.output
    assert "one" not in result.output


def test_gateway_logs_reports_missing_file(tmp_path: Path):
    workspace = tmp_path / ".ohmo-home"
    runner = CliRunner()
    result = runner.invoke(app, ["gateway", "logs", "--workspace", str(workspace)])
    assert result.exit_code == 1
    assert "No gateway log yet" in result.output
    assert "gateway.log" in result.output


def test_ohmo_backend_only_restores_snapshot_messages(tmp_path: Path, monkeypatch):
    runner = CliRunner()
    workspace = tmp_path / ".ohmo-home"
    assert runner.invoke(app, ["init", "--workspace", str(workspace), "--no-interactive"]).exit_code == 0
    session_dir = workspace / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "latest.json").write_text(
        json.dumps(
            {
                "session_id": "prev123",
                "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
                "tool_metadata": {"recent_work_log": ["edited x.py"]},
            }
        ),
        encoding="utf-8",
    )

    captured: dict[str, object] = {}

    async def _fake_backend(**kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr("ohmo.cli.run_ohmo_backend", _fake_backend)

    result = runner.invoke(
        app, ["--backend-only", "--continue", "--workspace", str(workspace), "--cwd", str(tmp_path)]
    )

    assert result.exit_code == 0
    restore_messages = captured["restore_messages"]
    assert isinstance(restore_messages, list)
    assert restore_messages[0]["role"] == "user"
    assert captured["restore_tool_metadata"] == {"recent_work_log": ["edited x.py"]}
