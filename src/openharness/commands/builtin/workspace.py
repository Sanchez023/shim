"""Workspace, plugin, GitHub, task, and autopilot slash commands."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from openharness.autopilot import RepoAutopilotStore
from openharness.bridge import get_bridge_manager
from openharness.bridge.types import WorkSecret
from openharness.bridge.work_secret import build_sdk_url, decode_work_secret, encode_work_secret
from openharness.commands.helpers import (
    _is_valid_skill_command_name,
    _register_user_invocable_skill_commands,
    _run_git_command,
    _shorten_text,
    _skill_command_name,
)
from openharness.commands.types import CommandContext, CommandResult, SlashCommand
from openharness.config.paths import get_project_issue_file, get_project_pr_comments_file
from openharness.config.settings import load_settings, save_settings
from openharness.plugins import load_plugins
from openharness.plugins.installer import install_plugin_from_path, uninstall_plugin
from openharness.services.task_follow import (
    AGENT_TASK_TYPES,
    active_task_follows,
    start_task_follow,
    stop_task_follow,
)
from openharness.skills import load_skill_registry
from openharness.tasks import get_task_manager
from openharness.tasks.summary import (
    format_task_summary,
    read_task_output_with_meta,
    truncation_note,
)

if TYPE_CHECKING:
    from openharness.commands.registry import CommandRegistry


def register_workspace_commands(registry: CommandRegistry) -> None:
    """Register the built-in workspace slash commands."""

    async def _agents_handler(args: str, context: CommandContext) -> CommandResult:
        tokens = args.split(maxsplit=1)
        guide = (
            "Subagent guide:\n"
            "- Ask the model to delegate with the `agent` tool when the task needs background work or parallel investigation.\n"
            '- The usual worker shape is subagent_type="worker".\n'
            "- /agents lists known worker tasks.\n"
            "- /agents show TASK_ID shows one worker's output and metadata.\n"
            "- /agents follow [TASK_ID] streams a worker's new log lines into this conversation (defaults to the newest running agent).\n"
            "- /agents unfollow [TASK_ID|all] stops streaming.\n"
            "- send_message(task_id=..., message=...) can continue a spawned worker.\n"
            "- task_output(task_id=...) reads the worker's latest output."
        )
        if tokens and tokens[0] in {"help", "usage"}:
            return CommandResult(
                message=guide
            )
        if tokens and tokens[0] == "follow":
            target = tokens[1].strip() if len(tokens) == 2 else ""
            if context.print_system is None:
                return CommandResult(
                    message="Following needs an interactive transcript; call task_output(task_id=...) instead."
                )
            if target:
                task = get_task_manager().get_task(target)
                if task is None or task.type not in AGENT_TASK_TYPES:
                    return CommandResult(message=f"No agent found with ID: {target}")
            else:
                running = [
                    task
                    for task in get_task_manager().list_tasks()
                    if task.type in AGENT_TASK_TYPES and task.status == "running"
                ]
                if not running:
                    return CommandResult(
                        message=(
                            "No running agents to follow. "
                            "Pass a task id: /agents follow TASK_ID"
                        )
                    )
                task = running[0]
            try:
                start_task_follow(task.id, context.print_system)
            except ValueError as exc:
                return CommandResult(message=str(exc))
            return CommandResult(
                message=(
                    f"Following {task.id}. New output appears in the transcript; "
                    f"stop with /agents unfollow {task.id}."
                )
            )
        if tokens and tokens[0] == "unfollow":
            target = tokens[1].strip() if len(tokens) == 2 else ""
            stopped = await stop_task_follow(
                None if target in {"", "all"} else target
            )
            if not stopped:
                return CommandResult(
                    message="No active follow for that task."
                )
            return CommandResult(
                message=f"Stopped following: {', '.join(stopped)}"
            )
        if tokens and tokens[0] == "show" and len(tokens) == 2:
            task = get_task_manager().get_task(tokens[1])
            if task is None or task.type not in AGENT_TASK_TYPES:
                return CommandResult(message=f"No agent found with ID: {tokens[1]}")
            manager = get_task_manager()
            output, total_chars, truncated = read_task_output_with_meta(manager, task.id)
            header = format_task_summary(task)
            if truncated:
                output = (
                    f"{truncation_note(max_bytes=12000, total_chars=total_chars)}\n"
                    f"{output}"
                )
            if not output.strip() and task.status == "running":
                output = "(no output yet — task is still running)"
            return CommandResult(
                message=(
                    f"{header}\n"
                    f"metadata={task.metadata}\n"
                    f"output:\n{output or '(no output)'}"
                )
            )
        tasks = [
            task
            for task in get_task_manager().list_tasks()
            if task.type in AGENT_TASK_TYPES
        ]
        if not tasks:
            return CommandResult(
                message=f"No active or recorded agents. Run /agents help for usage.\n\n{guide}"
            )
        followed = set(active_task_follows())
        lines = [
            f"{task.id} {task.type} {task.status} {task.description}"
            + (" (following)" if task.id in followed else "")
            for task in tasks
        ]
        lines.append(
            "Follow one with /agents follow TASK_ID; stop with /agents unfollow."
        )
        return CommandResult(message="\n".join(lines))

    async def _bridge_handler(args: str, context: CommandContext) -> CommandResult:
        tokens = args.split()
        if not tokens or tokens[0] == "show":
            sessions = get_bridge_manager().list_sessions()
            lines = [
                "Bridge summary:",
                "- backend host: available",
                f"- cwd: {context.cwd}",
                f"- sessions: {len(sessions)}",
                "- utilities: encode, decode, sdk, spawn, list, output, stop",
            ]
            return CommandResult(message="\n".join(lines))
        if tokens[0] == "encode" and len(tokens) == 3:
            encoded = encode_work_secret(
                WorkSecret(version=1, session_ingress_token=tokens[2], api_base_url=tokens[1])
            )
            return CommandResult(message=encoded)
        if tokens[0] == "decode" and len(tokens) == 2:
            secret = decode_work_secret(tokens[1])
            return CommandResult(message=json.dumps(secret.__dict__, indent=2))
        if tokens[0] == "sdk" and len(tokens) == 3:
            return CommandResult(message=build_sdk_url(tokens[1], tokens[2]))
        if tokens[0] == "spawn" and len(tokens) >= 2:
            command = args[len("spawn ") :]
            handle = await get_bridge_manager().spawn(
                session_id=f"bridge-{datetime.now(UTC).strftime('%H%M%S')}",
                command=command,
                cwd=context.cwd,
            )
            return CommandResult(
                message=f"Spawned bridge session {handle.session_id} pid={handle.process.pid}"
            )
        if tokens[0] == "list":
            sessions = get_bridge_manager().list_sessions()
            if not sessions:
                return CommandResult(message="No bridge sessions.")
            return CommandResult(
                message="\n".join(
                    f"{item.session_id} [{item.status}] pid={item.pid} {item.command}"
                    for item in sessions
                )
            )
        if tokens[0] == "output" and len(tokens) == 2:
            return CommandResult(message=get_bridge_manager().read_output(tokens[1]) or "(no output)")
        if tokens[0] == "stop" and len(tokens) == 2:
            try:
                await get_bridge_manager().stop(tokens[1])
            except ValueError as exc:
                return CommandResult(message=str(exc))
            return CommandResult(message=f"Stopped bridge session {tokens[1]}")
        return CommandResult(
            message="Usage: /bridge [show|encode API_BASE_URL TOKEN|decode SECRET|sdk API_BASE_URL SESSION_ID|spawn CMD|list|output SESSION_ID|stop SESSION_ID]"
        )

    async def _reload_plugins_handler(_: str, context: CommandContext) -> CommandResult:
        settings = load_settings()
        plugins = load_plugins(settings, context.cwd, extra_roots=context.extra_plugin_roots)
        if not plugins:
            return CommandResult(message="No plugins discovered.")
        lines = ["Reloaded plugins:"]
        for plugin in plugins:
            state = "enabled" if plugin.enabled else "disabled"
            lines.append(f"- {plugin.manifest.name} [{state}]")
        return CommandResult(message="\n".join(lines))

    async def _skills_handler(args: str, context: CommandContext) -> CommandResult:
        skill_registry = load_skill_registry(
            context.cwd,
            extra_skill_dirs=context.extra_skill_dirs,
            extra_plugin_roots=context.extra_plugin_roots,
        )
        if args:
            skill = skill_registry.get(args)
            if skill is None:
                return CommandResult(message=f"Skill not found: {args}")
            return CommandResult(message=skill.content)
        skills = skill_registry.list_skills()
        if not skills:
            return CommandResult(message="No skills available.")
        lines = ["Available skills:"]
        for skill in skills:
            source = f" [{skill.source}]"
            path = f" {skill.path}" if skill.path else ""
            command_name = _skill_command_name(skill)
            slash = f" /{command_name}" if skill.user_invocable and _is_valid_skill_command_name(command_name) else ""
            display = f" ({skill.display_name})" if skill.display_name else ""
            lines.append(f"- {command_name}{display}{source}{path}{slash}: {skill.description}")
        return CommandResult(message="\n".join(lines))

    async def _issue_handler(args: str, context: CommandContext) -> CommandResult:
        path = get_project_issue_file(context.cwd)
        tokens = args.split(maxsplit=1)
        action = tokens[0] if tokens else "show"
        rest = tokens[1] if len(tokens) == 2 else ""
        if action == "show":
            if not path.exists():
                return CommandResult(message=f"No issue context. File path: {path}")
            return CommandResult(message=path.read_text(encoding="utf-8"))
        if action == "set" and rest:
            title, separator, body = rest.partition("::")
            if not separator or not title.strip() or not body.strip():
                return CommandResult(message="Usage: /issue set TITLE :: BODY")
            content = f"# {title.strip()}\n\n{body.strip()}\n"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return CommandResult(message=f"Saved issue context to {path}")
        if action == "clear":
            if path.exists():
                path.unlink()
                return CommandResult(message="Cleared issue context.")
            return CommandResult(message="No issue context to clear.")
        return CommandResult(message="Usage: /issue [show|set TITLE :: BODY|clear]")

    async def _pr_comments_handler(args: str, context: CommandContext) -> CommandResult:
        path = get_project_pr_comments_file(context.cwd)
        tokens = args.split(maxsplit=1)
        action = tokens[0] if tokens else "show"
        rest = tokens[1] if len(tokens) == 2 else ""
        if action == "show":
            if not path.exists():
                return CommandResult(message=f"No PR comments context. File path: {path}")
            return CommandResult(message=path.read_text(encoding="utf-8"))
        if action == "add" and rest:
            location, separator, comment = rest.partition("::")
            if not separator or not location.strip() or not comment.strip():
                return CommandResult(message="Usage: /pr_comments add FILE[:LINE] :: COMMENT")
            existing = path.read_text(encoding="utf-8") if path.exists() else "# PR Comments\n"
            if not existing.endswith("\n"):
                existing += "\n"
            existing += f"- {location.strip()}: {comment.strip()}\n"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(existing, encoding="utf-8")
            return CommandResult(message=f"Added PR comment to {path}")
        if action == "clear":
            if path.exists():
                path.unlink()
                return CommandResult(message="Cleared PR comments context.")
            return CommandResult(message="No PR comments context to clear.")
        return CommandResult(message="Usage: /pr_comments [show|add FILE[:LINE] :: COMMENT|clear]")

    async def _mcp_handler(args: str, context: CommandContext) -> CommandResult:
        settings = load_settings()
        tokens = args.split()
        if tokens and tokens[0] == "auth" and len(tokens) >= 3:
            server_name = tokens[1]
            config = settings.mcp_servers.get(server_name)
            if config is None:
                return CommandResult(message=f"Unknown MCP server: {server_name}")

            if len(tokens) == 3:
                mode = "bearer"
                key = None
                value = tokens[2]
            elif len(tokens) == 4:
                mode = tokens[2]
                key = None
                value = tokens[3]
            elif len(tokens) == 5:
                mode = tokens[2]
                key = tokens[3]
                value = tokens[4]
            else:
                return CommandResult(
                    message="Usage: /mcp auth SERVER TOKEN | /mcp auth SERVER [bearer|env] VALUE | /mcp auth SERVER header KEY VALUE"
                )

            if hasattr(config, "headers"):
                if mode not in {"bearer", "header"}:
                    return CommandResult(message="HTTP/WS MCP auth supports bearer or header modes.")
                header_key = key or "Authorization"
                header_value = (
                    f"Bearer {value}" if mode == "bearer" and header_key == "Authorization" else value
                )
                headers = dict(getattr(config, "headers", {}) or {})
                headers[header_key] = header_value
                settings.mcp_servers[server_name] = config.model_copy(update={"headers": headers})
            elif hasattr(config, "env"):
                if mode not in {"bearer", "env"}:
                    return CommandResult(message="stdio MCP auth supports bearer or env modes.")
                env_key = key or "MCP_AUTH_TOKEN"
                env_value = f"Bearer {value}" if mode == "bearer" else value
                env = dict(getattr(config, "env", {}) or {})
                env[env_key] = env_value
                settings.mcp_servers[server_name] = config.model_copy(update={"env": env})
            else:
                return CommandResult(message=f"Server {server_name} does not support auth updates")
            save_settings(settings)
            return CommandResult(message=f"Saved MCP auth for {server_name}. Restart session to reconnect.")
        return CommandResult(message=context.mcp_summary or "No MCP servers configured.")

    async def _plugin_handler(args: str, context: CommandContext) -> CommandResult:
        settings = load_settings()
        tokens = args.split()
        if not tokens or tokens[0] == "list":
            return CommandResult(message=context.plugin_summary or "No plugins discovered.")
        if tokens[0] == "enable" and len(tokens) == 2:
            settings.enabled_plugins[tokens[1]] = True
            save_settings(settings)
            return CommandResult(message=f"Enabled plugin '{tokens[1]}'. Restart session to reload.")
        if tokens[0] == "disable" and len(tokens) == 2:
            settings.enabled_plugins[tokens[1]] = False
            save_settings(settings)
            return CommandResult(message=f"Disabled plugin '{tokens[1]}'. Restart session to reload.")
        if tokens[0] == "install" and len(tokens) == 2:
            path = install_plugin_from_path(tokens[1])
            return CommandResult(message=f"Installed plugin to {path}")
        if tokens[0] == "uninstall" and len(tokens) == 2:
            try:
                removed = uninstall_plugin(tokens[1])
            except ValueError:
                return CommandResult(message=f"Invalid plugin name '{tokens[1]}'")
            if removed:
                return CommandResult(message=f"Uninstalled plugin '{tokens[1]}'")
            return CommandResult(message=f"Plugin '{tokens[1]}' not found")
        plugins = load_plugins(settings, context.cwd, extra_roots=context.extra_plugin_roots)
        if plugins:
            return CommandResult(message=context.plugin_summary)
        return CommandResult(message="Usage: /plugin [list|enable NAME|disable NAME|install PATH|uninstall NAME]")

    async def _diff_handler(args: str, context: CommandContext) -> CommandResult:
        if args.strip() == "full":
            ok, output = _run_git_command(context.cwd, "diff", "HEAD")
            return CommandResult(message=output or "(no diff)")
        ok, output = _run_git_command(context.cwd, "diff", "--stat")
        if not ok:
            return CommandResult(message=output)
        return CommandResult(message=output or "(no diff)")

    async def _branch_handler(args: str, context: CommandContext) -> CommandResult:
        action = args.strip() or "show"
        if action == "show":
            ok, current = _run_git_command(context.cwd, "branch", "--show-current")
            if not ok:
                return CommandResult(message=current)
            return CommandResult(message=f"Current branch: {current or '(detached HEAD)'}")
        if action == "list":
            ok, branches = _run_git_command(context.cwd, "branch", "--format", "%(refname:short)")
            return CommandResult(message=branches if ok else branches)
        return CommandResult(message="Usage: /branch [show|list]")

    async def _commit_handler(args: str, context: CommandContext) -> CommandResult:
        message = args.strip()
        if not message:
            ok, status = _run_git_command(context.cwd, "status", "--short")
            return CommandResult(message=status if ok and status else "(working tree clean)")
        ok, status = _run_git_command(context.cwd, "status", "--short")
        if not ok:
            return CommandResult(message=status)
        if not status.strip():
            return CommandResult(message="Nothing to commit.")
        ok, output = _run_git_command(context.cwd, "add", "-A")
        if not ok:
            return CommandResult(message=output)
        ok, output = _run_git_command(context.cwd, "commit", "-m", message)
        return CommandResult(message=output if ok else output)

    async def _tasks_handler(args: str, context: CommandContext) -> CommandResult:
        manager = get_task_manager()
        tokens = args.split(maxsplit=2)
        if not tokens or tokens[0] == "list":
            tasks = manager.list_tasks()
            if not tasks:
                return CommandResult(message="No background tasks.")
            return CommandResult(
                message="\n".join(f"{task.id} {task.type} {task.status} {task.description}" for task in tasks)
            )
        if tokens[0] == "run" and len(tokens) >= 2:
            command = args[len("run ") :]
            task = await manager.create_shell_task(
                command=command,
                description=command[:80],
                cwd=context.cwd,
            )
            return CommandResult(message=f"Started task {task.id}")
        if tokens[0] == "stop" and len(tokens) == 2:
            task = await manager.stop_task(tokens[1])
            return CommandResult(message=f"Stopped task {task.id}")
        if tokens[0] == "show" and len(tokens) == 2:
            task = manager.get_task(tokens[1])
            if task is None:
                return CommandResult(message=f"No task found with ID: {tokens[1]}")
            return CommandResult(message=str(task))
        if tokens[0] == "update" and len(tokens) == 3:
            task_id = tokens[1]
            rest = tokens[2]
            field, _, value = rest.partition(" ")
            if not value.strip():
                return CommandResult(
                    message="Usage: /tasks update ID [description TEXT|progress NUMBER|note TEXT]"
                )
            try:
                if field == "description":
                    task = manager.update_task(task_id, description=value)
                    return CommandResult(message=f"Updated task {task.id} description")
                if field == "progress":
                    try:
                        progress = int(value)
                    except ValueError:
                        return CommandResult(message="Progress must be an integer between 0 and 100.")
                    task = manager.update_task(task_id, progress=progress)
                    return CommandResult(message=f"Updated task {task.id} progress to {progress}%")
                if field == "note":
                    task = manager.update_task(task_id, status_note=value)
                    return CommandResult(message=f"Updated task {task.id} note")
            except ValueError as exc:
                return CommandResult(message=str(exc))
            return CommandResult(
                message="Usage: /tasks update ID [description TEXT|progress NUMBER|note TEXT]"
            )
        if tokens[0] == "output" and len(tokens) == 2:
            return CommandResult(message=manager.read_task_output(tokens[1]) or "(no output)")
        return CommandResult(
            message=(
                "Usage: /tasks "
                "[list|run CMD|stop ID|show ID|update ID description TEXT|update ID progress NUMBER|update ID note TEXT|output ID]"
            )
        )

    async def _autopilot_handler(args: str, context: CommandContext) -> CommandResult:
        store = RepoAutopilotStore(context.cwd)
        tokens = args.split()
        action = tokens[0].lower() if tokens else "status"

        def _render_card(card) -> str:
            lines = [
                f"{card.id} [{card.status}] score={card.score} {card.title}",
                f"source={card.source_kind} ref={card.source_ref or '-'}",
            ]
            if card.labels:
                lines.append(f"labels={', '.join(card.labels)}")
            if card.score_reasons:
                lines.append(f"reasons={', '.join(card.score_reasons[:4])}")
            if card.body:
                lines.append(_shorten_text(card.body, limit=220))
            return "\n".join(lines)

        if action == "status":
            counts = store.stats()
            active = store.pick_next_card()
            lines = ["Autopilot queue status:"]
            for status_name in (
                "queued",
                "accepted",
                "preparing",
                "running",
                "verifying",
                "pr_open",
                "waiting_ci",
                "repairing",
                "completed",
                "merged",
                "failed",
                "rejected",
                "superseded",
            ):
                lines.append(f"- {status_name}: {counts.get(status_name, 0)}")
            lines.append(f"- registry: {store.registry_path}")
            lines.append(f"- journal: {store.journal_path}")
            lines.append(f"- context: {store.context_path}")
            if active is not None:
                lines.append(f"- next: {active.id} {active.title} (score={active.score})")
            return CommandResult(message="\n".join(lines))

        if action == "list":
            status = tokens[1].lower() if len(tokens) >= 2 else None
            if status is not None and status not in {
                "queued",
                "accepted",
                "preparing",
                "running",
                "verifying",
                "pr_open",
                "waiting_ci",
                "repairing",
                "completed",
                "merged",
                "failed",
                "rejected",
                "superseded",
            }:
                return CommandResult(message=f"Unknown autopilot status: {status}")
            cards = store.list_cards(status=status)
            if not cards:
                return CommandResult(message="No autopilot cards.")
            return CommandResult(message="\n\n".join(_render_card(card) for card in cards[:12]))

        if action == "show" and len(tokens) >= 2:
            card = store.get_card(tokens[1])
            if card is None:
                return CommandResult(message=f"No autopilot card found with ID: {tokens[1]}")
            return CommandResult(message=_render_card(card))

        if action == "next":
            card = store.pick_next_card()
            if card is None:
                return CommandResult(message="No queued autopilot cards.")
            return CommandResult(message=_render_card(card))

        if action == "context":
            content = store.load_active_context()
            return CommandResult(message=content or "Active repo context is empty.")

        if action == "journal":
            limit = 8
            if len(tokens) >= 2:
                try:
                    limit = max(1, min(30, int(tokens[1])))
                except ValueError:
                    return CommandResult(message="Usage: /autopilot journal [LIMIT]")
            entries = store.load_journal(limit=limit)
            if not entries:
                return CommandResult(message="Repo journal is empty.")
            lines = []
            for entry in entries:
                timestamp = datetime.fromtimestamp(entry.timestamp, tz=UTC).strftime(
                    "%Y-%m-%d %H:%M UTC"
                )
                task_suffix = f" [{entry.task_id}]" if entry.task_id else ""
                lines.append(f"{timestamp} {entry.kind}{task_suffix}: {entry.summary}")
            return CommandResult(message="\n".join(lines))

        if action == "add":
            raw = args[len("add") :].strip()
            if not raw:
                return CommandResult(
                    message=(
                        "Usage: /autopilot add "
                        "[idea|ohmo|issue|pr|claude] TITLE :: DETAILS"
                    )
                )
            source_kind = "manual_idea"
            source_map = {
                "idea": "manual_idea",
                "manual": "manual_idea",
                "ohmo": "ohmo_request",
                "issue": "github_issue",
                "pr": "github_pr",
                "claude": "claude_code_candidate",
            }
            if " " in raw:
                first, remainder = raw.split(" ", 1)
                mapped = source_map.get(first.lower())
                if mapped is not None:
                    source_kind = mapped
                    raw = remainder.strip()
            title, _, body = raw.partition("::")
            if not title.strip():
                return CommandResult(
                    message=(
                        "Usage: /autopilot add "
                        "[idea|ohmo|issue|pr|claude] TITLE :: DETAILS"
                    )
                )
            card, created = store.enqueue_card(
                source_kind=source_kind,
                title=title.strip(),
                body=body.strip(),
            )
            status_word = "Queued" if created else "Refreshed"
            return CommandResult(
                message=f"{status_word} autopilot card {card.id} (score={card.score}): {card.title}"
            )

        if action in {"accept", "start", "complete", "reject", "fail"} and len(tokens) >= 2:
            status_map = {
                "accept": "accepted",
                "start": "running",
                "complete": "completed",
                "fail": "failed",
                "reject": "rejected",
            }
            note = ""
            if len(tokens) >= 3:
                note = args.split(maxsplit=2)[2]
            try:
                card = store.update_status(tokens[1], status=status_map[action], note=note or None)
            except ValueError as exc:
                return CommandResult(message=str(exc))
            return CommandResult(message=f"{card.id} -> {card.status}: {card.title}")

        if action == "run-next":
            try:
                result = await store.run_next()
            except ValueError as exc:
                return CommandResult(message=str(exc))
            return CommandResult(
                message=(
                    f"{result.card_id} -> {result.status}\n"
                    f"run report: {result.run_report_path}\n"
                    f"verification report: {result.verification_report_path}"
                )
            )

        if action == "tick":
            try:
                result = await store.tick()
            except ValueError as exc:
                return CommandResult(message=str(exc))
            if result is None:
                return CommandResult(message="Autopilot tick completed with no execution.")
            return CommandResult(
                message=(
                    f"Autopilot tick executed {result.card_id} -> {result.status}\n"
                    f"run report: {result.run_report_path}\n"
                    f"verification report: {result.verification_report_path}"
                )
            )

        if action == "install-cron":
            names = store.install_default_cron()
            return CommandResult(message="Installed autopilot cron jobs: " + ", ".join(names))

        if action == "export-dashboard":
            output = tokens[1] if len(tokens) >= 2 else None
            path = store.export_dashboard(output)
            return CommandResult(message=f"Exported autopilot dashboard: {path}")

        if action == "scan":
            if len(tokens) < 2:
                return CommandResult(
                    message="Usage: /autopilot scan [issues|prs|claude-code|all] [LIMIT]"
                )
            target = tokens[1].lower()
            limit = 10
            if len(tokens) >= 3:
                try:
                    limit = max(1, min(50, int(tokens[2])))
                except ValueError:
                    return CommandResult(
                        message="Usage: /autopilot scan [issues|prs|claude-code|all] [LIMIT]"
                    )
            try:
                if target == "issues":
                    cards = store.scan_github_issues(limit=limit)
                    return CommandResult(message=f"Scanned {len(cards)} GitHub issues into autopilot.")
                if target == "prs":
                    cards = store.scan_github_prs(limit=limit)
                    return CommandResult(message=f"Scanned {len(cards)} GitHub PRs into autopilot.")
                if target == "claude-code":
                    cards = store.scan_claude_code_candidates(limit=limit)
                    return CommandResult(
                        message=f"Scanned {len(cards)} claude-code candidates into autopilot."
                    )
                if target == "all":
                    counts = store.scan_all_sources(issue_limit=limit, pr_limit=limit)
                    return CommandResult(message=f"Scanned all sources: {json.dumps(counts)}")
            except ValueError as exc:
                return CommandResult(message=str(exc))
            return CommandResult(
                message="Usage: /autopilot scan [issues|prs|claude-code|all] [LIMIT]"
            )

        return CommandResult(
            message=(
                "Usage: /autopilot "
                "[status|list [STATUS]|show ID|next|context|journal [LIMIT]|"
                "add [idea|ohmo|issue|pr|claude] TITLE :: DETAILS|"
                "accept ID|start ID|complete ID [NOTE]|fail ID [NOTE]|reject ID [NOTE]|"
                "run-next|tick|install-cron|export-dashboard [OUTPUT]|"
                "scan [issues|prs|claude-code|all] [LIMIT]]"
            )
        )

    async def _ship_handler(args: str, context: CommandContext) -> CommandResult:
        raw = args.strip()
        if not raw:
            return CommandResult(message="Usage: /ship TITLE :: DETAILS")
        title, _, body = raw.partition("::")
        if not title.strip():
            return CommandResult(message="Usage: /ship TITLE :: DETAILS")
        store = RepoAutopilotStore(context.cwd)
        card, _ = store.enqueue_card(
            source_kind="ohmo_request",
            title=title.strip(),
            body=body.strip(),
        )
        try:
            result = await store.run_card(card.id)
        except ValueError as exc:
            return CommandResult(message=str(exc))
        return CommandResult(
            message=(
                f"{result.card_id} -> {result.status}\n"
                f"run report: {result.run_report_path}\n"
                f"verification report: {result.verification_report_path}"
            )
        )

    registry.register(
        SlashCommand(
            "bridge",
            "Inspect bridge helpers and spawn bridge sessions",
            _bridge_handler,
            remote_invocable=False,
            remote_admin_opt_in=True,
        )
    )

    registry.register(SlashCommand("skills", "List or show available skills", _skills_handler, busy_safe=True))

    registry.register(
        SlashCommand(
            "mcp",
            "Show MCP status",
            _mcp_handler,
            remote_invocable=False,
            remote_admin_opt_in=True,
        )
    )

    registry.register(
        SlashCommand(
            "plugin",
            "Manage plugins",
            _plugin_handler,
            remote_invocable=False,
            remote_admin_opt_in=True,
        )
    )

    registry.register(
        SlashCommand(
            "reload-plugins",
            "Reload plugin discovery for this workspace",
            _reload_plugins_handler,
            remote_invocable=False,
            remote_admin_opt_in=True,
        )
    )

    registry.register(
        SlashCommand(
            "diff",
            "Show git diff output",
            _diff_handler,
            remote_invocable=False,
            remote_admin_opt_in=True,
            busy_safe=True,
        )
    )

    registry.register(SlashCommand("branch", "Show git branch information", _branch_handler, busy_safe=True))

    registry.register(
        SlashCommand(
            "commit",
            "Show status or create a git commit",
            _commit_handler,
            remote_invocable=False,
            remote_admin_opt_in=True,
        )
    )

    registry.register(
        SlashCommand(
            "issue",
            "Show or update project issue context",
            _issue_handler,
            remote_invocable=False,
            remote_admin_opt_in=True,
        )
    )

    registry.register(
        SlashCommand(
            "pr_comments",
            "Show or update project PR comments context",
            _pr_comments_handler,
            remote_invocable=False,
            remote_admin_opt_in=True,
        )
    )

    registry.register(SlashCommand("agents", "List or inspect agent and teammate tasks", _agents_handler))

    registry.register(SlashCommand("subagents", "Show subagent usage and inspect worker tasks", _agents_handler))

    registry.register(
        SlashCommand(
            "tasks",
            "Manage background tasks",
            _tasks_handler,
            remote_invocable=False,
            remote_admin_opt_in=True,
        )
    )

    registry.register(
        SlashCommand(
            "autopilot",
            "Manage repo autopilot intake and context",
            _autopilot_handler,
            remote_invocable=False,
            remote_admin_opt_in=True,
        )
    )

    registry.register(
        SlashCommand(
            "ship",
            "Queue and execute an ohmo-driven repo task",
            _ship_handler,
            remote_invocable=False,
            remote_admin_opt_in=True,
        )
    )

    _register_user_invocable_skill_commands(registry)
