"""Session, conversation, and transcript slash commands."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from openharness.auth.manager import AuthManager
from openharness.commands.helpers import _copy_to_clipboard, _last_message_text, _rewind_turns
from openharness.commands.memory import _memory_backend_for_context
from openharness.commands.types import CommandContext, CommandResult, SlashCommand
from openharness.config.paths import get_feedback_log_path, get_project_config_dir
from openharness.config.settings import load_settings
from openharness.domain.messages import ConversationMessage, sanitize_conversation_messages
from openharness.prompts import build_runtime_system_prompt
from openharness.services import (
    build_post_compact_messages,
    compact_conversation,
    compact_messages,
    estimate_conversation_tokens,
    estimate_tokens,
    summarize_messages,
)
from openharness.services.session_storage import (
    get_project_session_dir,
    resumable_tool_metadata,
)
from openharness.tasks import get_task_manager
from openharness.version import __version__

if TYPE_CHECKING:
    from openharness.commands.registry import CommandRegistry


def _format_rate(value: object) -> str:
    """Format an optional 0..1 hit rate as a percentage."""
    if value is None:
        return "n/a"
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return "n/a"


def _apply_resumed_work_state(context: CommandContext, snapshot: dict[str, Any]) -> None:
    """Carry persisted work state (read files, work log, focus) into the engine."""
    restored = resumable_tool_metadata(snapshot.get("tool_metadata"))
    if "plan_file" not in restored:
        # Snapshots that predate persisted plan files (or sessions that never
        # wrote a checklist) must not inherit the plan file of the session we
        # are switching away from.
        session_id = str(snapshot.get("session_id") or "").strip()
        if session_id:
            restored["plan_file"] = str(
                get_project_session_dir(context.cwd) / f"plan-{session_id}.md"
            )
    if restored:
        context.engine.tool_metadata.update(restored)


def register_session_commands(registry: CommandRegistry) -> None:
    """Register the built-in session slash commands."""

    async def _help_handler(_: str, context: CommandContext) -> CommandResult:
        del context
        return CommandResult(message=registry.help_text())

    async def _exit_handler(_: str, context: CommandContext) -> CommandResult:
        del context
        return CommandResult(should_exit=True)

    async def _clear_handler(_: str, context: CommandContext) -> CommandResult:
        context.engine.clear()
        return CommandResult(message="Conversation cleared.", clear_screen=True)

    async def _status_handler(_: str, context: CommandContext) -> CommandResult:
        usage = context.engine.total_usage
        state = context.app_state.get() if context.app_state is not None else None
        manager = AuthManager()
        return CommandResult(
            message=(
                f"Messages: {len(context.engine.messages)}\n"
                f"Usage: input={usage.input_tokens} output={usage.output_tokens}\n"
                f"Cache: read={usage.cache_read_input_tokens} "
                f"write={usage.cache_creation_input_tokens}\n"
                f"Profile: {manager.get_active_profile()}\n"
                f"Effort: {state.effort if state is not None else load_settings().effort}\n"
                f"Passes: {state.passes if state is not None else load_settings().passes}"
            )
        )

    async def _version_handler(_: str, context: CommandContext) -> CommandResult:
        del context
        return CommandResult(message=f"OpenHarness {__version__}")

    async def _context_handler(_: str, context: CommandContext) -> CommandResult:
        # Show exactly what the runtime is using: rebuilding from disk would
        # drop CLI --system-prompt, extra skill dirs, and active-file context.
        prompt = context.engine.system_prompt or "(no system prompt)"
        return CommandResult(message=prompt)

    async def _summary_handler(args: str, context: CommandContext) -> CommandResult:
        max_messages = 8
        if args:
            try:
                max_messages = max(1, int(args))
            except ValueError:
                return CommandResult(message="Usage: /summary [MAX_MESSAGES]")
        summary = summarize_messages(context.engine.messages, max_messages=max_messages)
        return CommandResult(message=summary or "No conversation content to summarize.")

    async def _compact_handler(args: str, context: CommandContext) -> CommandResult:
        preserve_recent = 6
        if args:
            try:
                preserve_recent = max(1, int(args))
            except ValueError:
                return CommandResult(message="Usage: /compact [PRESERVE_RECENT]")
        before = len(context.engine.messages)
        try:
            compacted_result = await compact_conversation(
                context.engine.messages,
                api_client=context.engine.api_client,
                model=context.engine.model,
                system_prompt=context.engine.system_prompt,
                preserve_recent=preserve_recent,
                trigger="manual",
            )
            compacted = build_post_compact_messages(compacted_result)
        except Exception:  # noqa: BLE001 - compaction fallback must not break /compact
            compacted = compact_messages(context.engine.messages, preserve_recent=preserve_recent)
        context.engine.load_messages(compacted)
        return CommandResult(
            message=f"Compacted conversation from {before} messages to {len(compacted)}."
        )

    async def _usage_handler(_: str, context: CommandContext) -> CommandResult:
        usage = context.engine.total_usage
        estimated = estimate_conversation_tokens(context.engine.messages)
        system_prompt = context.engine.system_prompt or ""
        system_tokens = estimate_tokens(system_prompt)
        tool_schemas = context.engine.visible_tool_schemas()
        tools_json = json.dumps(tool_schemas, ensure_ascii=False) if tool_schemas else ""
        tool_tokens = estimate_tokens(tools_json)
        total_tokens = system_tokens + tool_tokens + estimated
        cache = context.engine.cache_summary
        hit_rate = _format_rate(cache.get("cache_hit_rate"))
        hot_rate = _format_rate(cache.get("hot_cache_hit_rate"))
        warmup = context.engine.warmup_stats
        warmup_line = ""
        if warmup is not None:
            warmup_line = (
                f"Warmup (excluded from hit rate): attempts={warmup['attempts']} "
                f"skipped={warmup['skipped']} failures={warmup['failures']} "
                f"input={warmup['input_tokens']} output={warmup['output_tokens']}\n"
            )
        keepalive = context.engine.keepalive_stats
        if keepalive is not None:
            warmup_line += (
                f"Cache keepalive: refreshes={keepalive['refreshes']} "
                f"busy-skips={keepalive['skipped_busy']} "
                f"failures={keepalive['failures']} "
                f"idle={keepalive['idle_seconds']:.0f}s\n"
            )
        return CommandResult(
            message=(
                f"Actual usage: input={usage.input_tokens} output={usage.output_tokens}\n"
                f"Cache usage: read={usage.cache_read_input_tokens} "
                f"write={usage.cache_creation_input_tokens}\n"
                f"Cache hit rate: {hit_rate} "
                f"(hot {hot_rate}, cold starts {cache.get('cold_starts', 0)} "
                f"provider-cold {cache.get('provider_cold_starts', 0)})\n"
                f"Cache drift: system={cache.get('system_drifts', 0)} "
                f"tools={cache.get('tool_drifts', 0)} "
                f"rewrites={cache.get('history_rewrites', 0)} "
                f"anomalies={cache.get('anomaly_misses', 0)} "
                f"alerts={cache.get('alerts', 0)}\n"
                f"{warmup_line}"
                "Estimated context breakdown:\n"
                f"- system prompt: ~{system_tokens} tokens ({len(system_prompt)} chars)\n"
                f"- tools: ~{tool_tokens} tokens "
                f"({len(tool_schemas)} tools, {len(tools_json)} chars)\n"
                f"- conversation: ~{estimated} tokens\n"
                f"- total: ~{total_tokens} tokens\n"
                f"Messages: {len(context.engine.messages)}"
            )
        )

    async def _cost_handler(_: str, context: CommandContext) -> CommandResult:
        usage = context.engine.total_usage
        model = context.app_state.get().model if context.app_state is not None else load_settings().model
        estimated_cost = "unavailable"
        if model.startswith(("claude-3-5-sonnet", "claude-3-7-sonnet")):
            estimated = (usage.input_tokens * 3.0 + usage.output_tokens * 15.0) / 1_000_000
            estimated_cost = f"${estimated:.4f} (estimated)"
        elif model.startswith("claude-3-opus"):
            estimated = (usage.input_tokens * 15.0 + usage.output_tokens * 75.0) / 1_000_000
            estimated_cost = f"${estimated:.4f} (estimated)"
        return CommandResult(
            message=(
                f"Model: {model}\n"
                f"Input tokens: {usage.input_tokens}\n"
                f"Output tokens: {usage.output_tokens}\n"
                f"Total tokens: {usage.total_tokens}\n"
                f"Estimated cost: {estimated_cost}"
            )
        )

    async def _stats_handler(_: str, context: CommandContext) -> CommandResult:
        settings = load_settings()
        memory_backend = _memory_backend_for_context(context)
        memory_count = len(memory_backend.list_files())
        task_count = len(get_task_manager().list_tasks())
        tool_count = len(context.tool_registry.list_tools()) if context.tool_registry is not None else 0
        style = settings.output_style
        if context.app_state is not None:
            state = context.app_state.get()
            style = state.output_style
        return CommandResult(
            message=(
                "Session stats:\n"
                f"- messages: {len(context.engine.messages)}\n"
                f"- estimated_tokens: {estimate_conversation_tokens(context.engine.messages)}\n"
                f"- tools: {tool_count}\n"
                f"- memory_files: {memory_count}\n"
                f"- background_tasks: {task_count}\n"
                f"- output_style: {style}"
            )
        )

    async def _hooks_handler(_: str, context: CommandContext) -> CommandResult:
        return CommandResult(message=context.hooks_summary or "No hooks configured.")

    async def _resume_handler(args: str, context: CommandContext) -> CommandResult:
        tokens = args.strip().split()

        # /resume <session_id> — load a specific session
        if tokens:
            sid = tokens[0]
            snapshot = context.session_backend.load_by_id(context.cwd, sid)
            if snapshot is None:
                return CommandResult(message=f"Session not found: {sid}")
            messages = sanitize_conversation_messages(
                [ConversationMessage.model_validate(item) for item in snapshot.get("messages", [])]
            )
            context.engine.load_messages(messages)
            _apply_resumed_work_state(context, snapshot)
            summary = snapshot.get("summary", "")[:60]
            return CommandResult(
                message=f"Restored {len(messages)} messages from session {sid}"
                + (f" ({summary})" if summary else ""),
                replay_messages=messages,
            )

        # /resume — list sessions (for the TUI to show a picker)
        sessions = context.session_backend.list_snapshots(context.cwd, limit=10)
        if not sessions:
            # Fall back to latest.json
            snapshot = context.session_backend.load_latest(context.cwd)
            if snapshot is None:
                return CommandResult(message="No saved sessions found for this project.")
            messages = sanitize_conversation_messages(
                [ConversationMessage.model_validate(item) for item in snapshot.get("messages", [])]
            )
            context.engine.load_messages(messages)
            _apply_resumed_work_state(context, snapshot)
            return CommandResult(
                message=f"Restored {len(messages)} messages from the latest session.",
                replay_messages=messages,
            )

        # Format session list for display / picker
        import time
        lines = ["Saved sessions:"]
        for s in sessions:
            ts = time.strftime("%m/%d %H:%M", time.localtime(s["created_at"]))
            summary = s["summary"][:50] or "(no summary)"
            lines.append(f"  {s['session_id']}  {ts}  {s['message_count']}msg  {summary}")
        lines.append("")
        lines.append("Use /resume <session_id> to restore a specific session.")
        return CommandResult(message="\n".join(lines))

    async def _export_handler(_: str, context: CommandContext) -> CommandResult:
        path = context.session_backend.export_markdown(cwd=context.cwd, messages=context.engine.messages)
        return CommandResult(message=f"Exported transcript to {path}")

    async def _share_handler(_: str, context: CommandContext) -> CommandResult:
        path = context.session_backend.export_markdown(cwd=context.cwd, messages=context.engine.messages)
        return CommandResult(message=f"Created shareable transcript snapshot at {path}")

    async def _copy_handler(args: str, context: CommandContext) -> CommandResult:
        text = args.strip() or _last_message_text(context.engine.messages)
        if not text:
            return CommandResult(message="Nothing to copy.")
        copied, target = _copy_to_clipboard(text)
        if copied:
            return CommandResult(message=f"Copied {len(text)} characters to the clipboard.")
        return CommandResult(message=f"Clipboard unavailable. Saved copied text to {target}")

    async def _session_handler(args: str, context: CommandContext) -> CommandResult:
        session_dir = context.session_backend.get_session_dir(context.cwd)
        tokens = args.split()
        if not tokens or tokens[0] == "show":
            latest = session_dir / "latest.json"
            transcript = session_dir / "transcript.md"
            lines = [
                f"Session ID: {context.session_id or '(none)'}",
                f"Session directory: {session_dir}",
                f"Latest snapshot: {'present' if latest.exists() else 'missing'}",
                f"Transcript export: {'present' if transcript.exists() else 'missing'}",
                f"Message count: {len(context.engine.messages)}",
            ]
            return CommandResult(message="\n".join(lines))
        if tokens[0] == "ls":
            files = sorted(path.name for path in session_dir.iterdir())
            return CommandResult(message="\n".join(files) if files else "(empty)")
        if tokens[0] == "path":
            return CommandResult(message=str(session_dir))
        if tokens[0] == "tag" and len(tokens) == 2:
            safe_name = "".join(character for character in tokens[1] if character.isalnum() or character in {"-", "_"})
            if not safe_name:
                return CommandResult(message="Usage: /session tag NAME")
            snapshot_path = context.session_backend.save_snapshot(
                cwd=context.cwd,
                model=context.app_state.get().model if context.app_state is not None else load_settings().model,
                system_prompt=build_runtime_system_prompt(
                    load_settings(),
                    cwd=context.cwd,
                    include_project_memory=context.include_project_memory,
                ),
                messages=context.engine.messages,
                usage=context.engine.total_usage,
            )
            export_path = context.session_backend.export_markdown(cwd=context.cwd, messages=context.engine.messages)
            tagged_json = session_dir / f"{safe_name}.json"
            tagged_md = session_dir / f"{safe_name}.md"
            shutil.copy2(snapshot_path, tagged_json)
            shutil.copy2(export_path, tagged_md)
            return CommandResult(message=f"Tagged session as {safe_name}:\n- {tagged_json}\n- {tagged_md}")
        if tokens[0] == "clear":
            if session_dir.exists():
                shutil.rmtree(session_dir)
            session_dir.mkdir(parents=True, exist_ok=True)
            return CommandResult(message=f"Cleared session storage at {session_dir}")
        return CommandResult(message="Usage: /session [show|ls|path|tag NAME|clear]")

    async def _rewind_handler(args: str, context: CommandContext) -> CommandResult:
        turns = 1
        if args.strip():
            try:
                turns = max(1, int(args.strip()))
            except ValueError:
                return CommandResult(message="Usage: /rewind [TURNS]")
        before = len(context.engine.messages)
        updated = _rewind_turns(context.engine.messages, turns)
        context.engine.load_messages(updated)
        removed = before - len(updated)
        return CommandResult(message=f"Rewound {turns} turn(s); removed {removed} message(s).")

    async def _tag_handler(args: str, context: CommandContext) -> CommandResult:
        name = args.strip()
        if not name:
            return CommandResult(message="Usage: /tag NAME")
        return await _session_handler(f"tag {name}", context)

    async def _files_handler(args: str, context: CommandContext) -> CommandResult:
        raw = args.strip()
        root = Path(context.cwd)
        max_items = 30
        tokens = raw.split(maxsplit=1)
        if tokens and tokens[0] == "dirs":
            dirs = [
                path
                for path in sorted(root.rglob("*"))
                if path.is_dir() and ".git" not in path.parts and ".venv" not in path.parts
            ]
            lines = [str(path.relative_to(root)) for path in dirs[:max_items]]
            if len(dirs) > max_items:
                lines.append(f"... {len(dirs) - max_items} more")
            return CommandResult(message="\n".join(lines) if lines else "(no directories)")
        if tokens and tokens[0].isdigit():
            max_items = max(1, min(int(tokens[0]), 200))
            raw = tokens[1] if len(tokens) == 2 else ""
        needle = raw.lower()
        files = [
            path
            for path in sorted(root.rglob("*"))
            if path.is_file() and ".git" not in path.parts and ".venv" not in path.parts
        ]
        if needle:
            files = [path for path in files if needle in str(path.relative_to(root)).lower()]
        lines = [str(path.relative_to(root)) for path in files[:max_items]]
        if len(files) > max_items:
            lines.append(f"... {len(files) - max_items} more")
        return CommandResult(
            message="\n".join(lines) if lines else "(no matching files)"
        )

    async def _init_handler(args: str, context: CommandContext) -> CommandResult:
        del args
        project_dir = get_project_config_dir(context.cwd)
        created: list[str] = []

        claudemd = Path(context.cwd) / "CLAUDE.md"
        if not claudemd.exists():
            claudemd.write_text(
                "# Project Instructions\n\n"
                "- Use OpenHarness tools deliberately.\n"
                "- Keep changes minimal and verify with tests when possible.\n",
                encoding="utf-8",
            )
            created.append(str(claudemd.relative_to(Path(context.cwd))))

        for relative, content in (
            (
                project_dir / "README.md",
                "# Project OpenHarness Config\n\nThis directory stores project-specific OpenHarness state.\n",
            ),
            (
                project_dir / "memory" / "MEMORY.md",
                "# Project Memory\n\nAdd reusable project knowledge here.\n",
            ),
            (
                project_dir / "plugins" / ".gitkeep",
                "",
            ),
            (
                project_dir / "skills" / ".gitkeep",
                "",
            ),
        ):
            relative.parent.mkdir(parents=True, exist_ok=True)
            if not relative.exists():
                relative.write_text(content, encoding="utf-8")
                created.append(str(relative.relative_to(Path(context.cwd))))

        if not created:
            return CommandResult(message="Project already initialized for OpenHarness.")
        return CommandResult(message="Initialized project files:\n" + "\n".join(f"- {item}" for item in created))

    async def _feedback_handler(args: str, context: CommandContext) -> CommandResult:
        del context
        path = get_feedback_log_path()
        if not args.strip():
            return CommandResult(message=f"Feedback log: {path}\nUsage: /feedback TEXT")
        timestamp = datetime.now(UTC).isoformat()
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{timestamp}] {args.strip()}\n")
        return CommandResult(message=f"Saved feedback to {path}")

    async def _onboarding_handler(_: str, context: CommandContext) -> CommandResult:
        del context
        return CommandResult(
            message=(
                "OpenHarness quickstart:\n"
                "1. Ask for a coding task in plain language.\n"
                "2. Use /help to inspect commands.\n"
                "3. Use /doctor to inspect runtime state.\n"
                "4. Use /tasks for background work and /memory for project memory.\n"
                "5. Use /login to store an API key if needed."
            )
        )

    async def _continue_handler(args: str, context: CommandContext) -> CommandResult:
        raw = args.strip()
        if not context.engine.has_pending_continuation():
            return CommandResult(message="Nothing to continue (no pending tool results).")

        turns: int | None = None
        if raw:
            tokens = raw.split()
            if tokens[0] == "set" and len(tokens) == 2:
                raw = tokens[1]
            try:
                turns = int(raw)
            except ValueError:
                return CommandResult(message="Usage: /continue [COUNT]")
            turns = max(1, min(turns, 512))

        return CommandResult(
            message="Continuing pending tool loop...",
            continue_pending=True,
            continue_turns=turns,
        )

    async def _stop_handler(_: str, _context: CommandContext) -> CommandResult:
        return CommandResult(
            message=(
                "No active turn is running in this command handler. "
                "While the TUI is running, type /stop or press Esc/Ctrl+C to interrupt the current turn. "
                "In ohmo remote channels, send /stop."
            )
        )

    async def _loop_handler(args: str, context: CommandContext) -> CommandResult:
        from openharness.services.cron import (
            delete_cron_job,
            get_cron_job,
            load_cron_jobs,
            upsert_cron_job,
        )
        from openharness.services.cron_scheduler import (
            is_scheduler_running,
            scheduler_status,
            start_daemon,
        )
        from openharness.services.loop import format_loop_summary, interval_to_cron

        settings = load_settings()
        parts = args.strip().split(maxsplit=1)
        action = parts[0].lower() if parts else "status"

        def _loop_jobs() -> list[dict[str, object]]:
            return [
                job
                for job in load_cron_jobs()
                if job.get("loop") is True
                or (isinstance(job.get("payload"), dict) and job["payload"].get("kind") == "shim_turn")
            ]

        if action in {"status", ""}:
            status = scheduler_status()
            jobs = _loop_jobs()
            state = "running" if status["running"] else "stopped"
            lines = [
                f"Loop scheduler: {state}"
                + (f" (pid={status['pid']})" if status.get("pid") else ""),
                f"Active loop jobs: {len(jobs)}",
            ]
            lines.extend(format_loop_summary(job) for job in jobs)
            if not status["running"] and jobs:
                lines.append("Scheduler is stopped; run `oh cron start` or create a new /loop.")
            return CommandResult(message="\n".join(lines))

        if action == "list":
            jobs = _loop_jobs()
            if not jobs:
                return CommandResult(message="No loop jobs. Create one with /loop 5m <prompt>.")
            return CommandResult(
                message="\n".join(["Loop jobs:", *(format_loop_summary(job) for job in jobs)])
            )

        if action == "stop":
            target = parts[1].strip() if len(parts) > 1 else "all"
            jobs = _loop_jobs()
            if target == "all":
                removed = [str(job.get("name")) for job in jobs if delete_cron_job(str(job.get("name")))]
                if not removed:
                    return CommandResult(message="No loop jobs to stop.")
                return CommandResult(
                    message="Stopped loop job(s): " + ", ".join(removed)
                )
            if target not in {str(job.get("name")) for job in jobs}:
                return CommandResult(message=f"Loop job not found: {target}")
            delete_cron_job(target)
            return CommandResult(message=f"Stopped loop job: {target}")

        interval = parts[0]
        prompt = parts[1].strip() if len(parts) > 1 else ""
        if not prompt:
            return CommandResult(
                message=(
                    "Usage: /loop <interval> <prompt>\n"
                    "Examples: /loop 5m check the build; /loop 1h summarize new issues\n"
                    "Also: /loop status|list|stop [NAME|all]"
                )
            )
        try:
            schedule = interval_to_cron(interval)
        except ValueError as exc:
            return CommandResult(message=f"/loop: {exc}")

        name = f"loop-{uuid4().hex[:8]}"
        job = {
            "name": name,
            "schedule": schedule,
            "cwd": context.cwd,
            "enabled": True,
            "loop": True,
            "payload": {
                "kind": "shim_turn",
                "message": prompt,
                "model": context.engine.model,
                "effort": settings.effort,
                "personality": settings.personality,
                "permission_mode": settings.permission.mode.value,
                "tool_profile": settings.tool_profile,
            },
        }
        upsert_cron_job(job)
        started = False
        scheduler_error = ""
        if not is_scheduler_running():
            try:
                start_daemon()
                started = True
            except Exception as exc:  # noqa: BLE001 - report scheduler startup failure
                scheduler_error = str(exc)

        stored = get_cron_job(name) or job
        next_run = str(stored.get("next_run") or "n/a")[:19]
        scheduler_line = (
            "Scheduler: started"
            if started
            else "Scheduler: running"
            if is_scheduler_running()
            else f"Scheduler: not running ({scheduler_error or 'run `oh cron start`'})"
        )
        return CommandResult(
            message=(
                f"Loop created: {name}\n"
                f"Schedule: {schedule} (every {interval})\n"
                f"Next run: {next_run}\n"
                f"{scheduler_line}\n"
                "Each run is a separate `python -m openharness --print ...` turn; "
                "results go to `oh cron history` / `oh cron logs`, not this conversation.\n"
                f"Stop with /loop stop {name} (or /loop stop all)."
            )
        )

    registry.register(SlashCommand("help", "Show available commands", _help_handler, busy_safe=True))

    registry.register(
        SlashCommand("exit", "Exit OpenHarness", _exit_handler, aliases=("quit",))
    )

    registry.register(SlashCommand("clear", "Clear conversation history", _clear_handler))

    registry.register(SlashCommand("version", "Show the installed OpenHarness version", _version_handler, busy_safe=True))

    registry.register(SlashCommand("status", "Show session status", _status_handler, busy_safe=True))

    registry.register(SlashCommand("context", "Show the active runtime system prompt", _context_handler, busy_safe=True))

    registry.register(
        SlashCommand(
            "summary",
            "Summarize conversation history",
            _summary_handler,
            remote_invocable=False,
            remote_admin_opt_in=True,
        )
    )

    registry.register(SlashCommand("compact", "Compact older conversation history", _compact_handler))

    registry.register(SlashCommand("cost", "Show token usage and estimated cost", _cost_handler, busy_safe=True))

    registry.register(SlashCommand("usage", "Show usage and token estimates", _usage_handler, busy_safe=True))

    registry.register(SlashCommand("stats", "Show session statistics", _stats_handler, busy_safe=True))

    registry.register(SlashCommand("hooks", "Show configured hooks", _hooks_handler, busy_safe=True))

    registry.register(
        SlashCommand(
            "resume",
            "Restore the latest saved session",
            _resume_handler,
            remote_invocable=False,
            remote_admin_opt_in=True,
        )
    )

    registry.register(SlashCommand("session", "Inspect the current session storage", _session_handler))

    registry.register(SlashCommand("export", "Export the current transcript", _export_handler, busy_safe=True))

    registry.register(SlashCommand("share", "Create a shareable transcript snapshot", _share_handler))

    registry.register(SlashCommand("copy", "Copy the latest response or provided text", _copy_handler, busy_safe=True))

    registry.register(SlashCommand("tag", "Create a named snapshot of the current session", _tag_handler, busy_safe=True))

    registry.register(SlashCommand("rewind", "Remove the latest conversation turn(s)", _rewind_handler))

    registry.register(SlashCommand("files", "List files in the current workspace", _files_handler, busy_safe=True))

    registry.register(SlashCommand("init", "Initialize project OpenHarness files", _init_handler))

    registry.register(SlashCommand("feedback", "Save CLI feedback to the local feedback log", _feedback_handler, busy_safe=True))

    registry.register(SlashCommand("onboarding", "Show the quickstart guide", _onboarding_handler, busy_safe=True))

    registry.register(SlashCommand("continue", "Continue the previous tool loop if it was interrupted", _continue_handler))
    registry.register(
        SlashCommand(
            "loop",
            "Run an agent prompt on a recurring interval",
            _loop_handler,
        )
    )

    registry.register(SlashCommand("stop", "Interrupt the running turn from TUI/ohmo channels", _stop_handler))
