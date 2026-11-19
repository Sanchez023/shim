"""Higher-level system prompt assembly."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from openharness.config.paths import (
    get_project_active_repo_context_path,
    get_project_issue_file,
    get_project_pr_comments_file,
)
from openharness.config.settings import Settings
from openharness.coordinator.coordinator_mode import (
    get_coordinator_system_prompt,
    is_coordinator_mode,
)
from openharness.memory import load_memory_prompt
from openharness.memory.relevance import format_relevant_memories, select_relevant_memories
from openharness.memory.usage import mark_memory_used
from openharness.permissions.modes import PermissionMode
from openharness.personality import build_personality_section
from openharness.personalization.rules import load_local_rules
from openharness.prompts.claudemd import load_project_rules_prompt_parts
from openharness.prompts.composer import (
    DEFAULT_MAX_PROMPT_CHARS,
    PromptBuildResult,
    PromptComposer,
    PromptSection,
)
from openharness.prompts.profiles import build_profile_section
from openharness.prompts.system_prompt import build_system_prompt_parts
from openharness.skills.loader import load_skill_registry


def _build_skills_section(
    cwd: str | Path,
    *,
    extra_skill_dirs: Iterable[str | Path] | None = None,
    extra_plugin_roots: Iterable[str | Path] | None = None,
    settings: Settings | None = None,
) -> str | None:
    """Build a system prompt section listing available skills."""
    registry = load_skill_registry(
        cwd,
        extra_skill_dirs=extra_skill_dirs,
        extra_plugin_roots=extra_plugin_roots,
        settings=settings,
    )
    skills = [skill for skill in registry.list_skills() if not skill.disable_model_invocation]
    if not skills:
        return None
    lines = [
        "# Available Skills",
        "",
        (
            "The following skills are available via the `skill` tool. "
            "When a user's request matches a skill, invoke it with `skill(name=\"<skill_name>\")` "
            "to load detailed instructions before proceeding. "
            "User-invocable skills can also be run directly by the user as `/<skill-name>`."
        ),
        "",
    ]
    for skill in skills:
        command_name = skill.command_name or skill.name
        display = f" ({skill.display_name})" if skill.display_name else ""
        lines.append(f"- **{command_name}**{display}: {skill.description}")
    return "\n".join(lines)


def _build_delegation_section(available_tools: set[str] | None = None) -> str | None:
    """Build a concise section describing delegation and worker usage."""
    if available_tools is not None and "agent" not in available_tools:
        return None

    lines = [
        "# Delegation And Subagents",
        "",
        "OpenHarness can delegate background work with the `agent` tool.",
        "Use subagents proactively when:",
        "- two or more independent investigations or file areas can run in parallel;",
        "- a task is long-running or can proceed in the background;",
        "- implementation and verification can be split;",
        "- a search spans three or more files/directories, an unfamiliar subsystem, or an open-ended \"where does X happen\" question;",
        "- a turn needs three or more independent searches, fetches, greps, or broad file reads;",
        "- research spans multiple sources or needs cross-checking across several areas;",
        "- the user asks for a subagent, worker, team, or parallel exploration.",
        "Delegating exploration keeps large search results out of the main context, which is faster and cheaper than reading everything yourself.",
        "Default to delegating for broad exploration: if you are about to scan an unfamiliar area or make three or more broad search/read/grep calls, send one or more Explore agents in the same response instead of doing the batch yourself.",
        "Do not delegate targeted reads of files you already know you will edit; read them directly, and issue all of those reads in one response.",
        "Do not spawn a subagent for simple single-file edits or questions you can answer directly.",
        "",
        "Default patterns:",
        '- `agent(description=..., prompt=..., subagent_type="worker")` for implementation work.',
        '- `subagent_type="Explore"` for read-only codebase research.',
        '- `subagent_type="Plan"` for design and planning.',
        '- `subagent_type="verification"` for post-change build/test/lint verification.',
        "- Spawn multiple workers in one response when their tasks are independent.",
        "- For broad research, ask Explore agents for conclusions plus `file:line` references, not file dumps, and launch 2–4 of them in one response when the areas are independent.",
        "- `Explore` and `Plan` calls wait for the agent and return its result directly; no polling is needed.",
        "- `worker` and `verification` calls run in the background; collect them with `task_output(task_id=..., block=true)` when you need the result.",
        "",
        "After spawning background workers:",
        "- Continue useful work instead of blocking on the worker.",
        "- Inspect running or recorded workers with `/agents`.",
        "- Inspect one worker in detail with `/agents show TASK_ID`.",
        "- Send follow-up instructions with `send_message(task_id=..., message=...)`.",
        "- Read worker output with `task_output(task_id=...)` or `task_output(task_id=..., block=true)` when you need the final result.",
    ]
    if available_tools is not None and "subagent_codex" in available_tools:
        lines.extend(
            [
                "",
                "External Codex delegation:",
                "- `subagent_codex(task=..., model=...)` runs one self-contained task in a fresh unattended Codex thread.",
                "- Prefer it for Codex-specific execution or an independent second implementation path.",
            ]
        )
    return "\n".join(lines)


_CHECKLIST_HYGIENE = (
    "Checklist hygiene (every mode): once an item is finished, close it immediately with "
    "`todo_write(item=<the text you used to create it>, checked=true)` — restate the item "
    "verbatim rather than rephrasing it, and prefer many small updates over one batch at the "
    "end. Before you tell the user the work is complete, re-read the checklist you own and "
    "close or rewrite every item still open; a stale unchecked entry is worse than no checklist."
)


def _build_permission_mode_section(settings: Settings) -> str:
    """Build current permission-mode guidance for the model."""
    mode = settings.permission.mode
    if mode == PermissionMode.PLAN:
        guidance = (
            "Plan mode is enabled. Treat this session as read-only planning and analysis. "
            "Do not call mutating tools such as file writes, edits, package installs, "
            "state-changing shell commands, or task-spawning actions unless the user exits plan mode. "
            "Before exploring or drafting the plan, identify what is still unspecified and confirm "
            "anything material instead of guessing: use `ask_user_question` to settle scope, "
            "expected behavior, constraints, priorities, or acceptance criteria. "
            "Ask one focused question per call; pass a short `options` list "
            "when the alternatives are clear (the UI always offers a custom answer). "
            "Do not ask about facts you can determine by reading the code or running read-only "
            "commands, and do not ask about details the user already specified. "
            "Record the plan as a markdown checklist with the `todo_write` tool once the goal "
            "and key decisions are settled. Immediately after finishing each item, call "
            "`todo_write(item=<exact item text>, checked=true)` so the sidebar status updates "
            "before you move on — do not batch the status updates until the end. The harness "
            "keeps the plan in this session's plan file (shown in the sidebar) rather than in "
            "the repository's TODO.md. "
            "If the user asks you to implement while plan mode is active, present the finished plan "
            "and ask them to exit plan mode (Shift+Tab or /permissions); you may call `exit_plan_mode`, "
            "which will ask for their confirmation and resume in auto mode."
        )
    elif mode == PermissionMode.FULL_AUTO:
        guidance = (
            "Full-auto permission mode is enabled. You may use mutating tools when they are necessary "
            "for the user's request, while still keeping changes scoped and intentional. "
            "When the user asks for a plan, design review, or analysis that should not change the "
            "workspace, call `enter_plan_mode` before doing the work; plan mode is read-only and "
            "surfaces the checklist sidebar. When the plan is ready and the user wants implementation, "
            "call `exit_plan_mode` (the user is asked to confirm) to resume in auto mode."
        )
    else:
        guidance = (
            "Default permission mode is enabled. Read-only tools can run directly; mutating tools "
            "may require explicit user approval. If the user asks for a plan, design, or analysis "
            "without implementation, call `enter_plan_mode` to switch to read-only planning first."
        )
    return f"# Current Permission Mode\n{guidance}\n\n{_CHECKLIST_HYGIENE}"


def _stable_sections(
    settings: Settings,
    *,
    cwd: str | Path,
    tool_names: set[str] | None,
    coordinator: bool,
    extra_skill_dirs: Iterable[str | Path] | None,
    extra_plugin_roots: Iterable[str | Path] | None,
) -> tuple[list[PromptSection], str]:
    """Build the session-stable prompt sections plus the live environment text."""
    live_environment = ""
    if coordinator:
        base_prompt = get_coordinator_system_prompt()
    else:
        parts = build_system_prompt_parts(
            custom_prompt=settings.system_prompt,
            cwd=str(cwd),
        )
        # Stable environment fields stay inside the cached prefix.
        base_prompt = parts.stable
        # cwd/date/branch are returned separately for the turn context.
        live_environment = parts.live

    sections: list[PromptSection] = [
        PromptSection(
            name="base",
            content=base_prompt,
            order=0,
            cacheable=True,
            required=True,
        )
    ]

    if not coordinator:
        personality_section = build_personality_section(
            settings.personality,
            settings.personality_custom,
        )
        if personality_section:
            sections.append(
                PromptSection(
                    name="personality",
                    content=personality_section,
                    order=2,
                    cacheable=True,
                )
            )

    if not coordinator and settings.system_prompt is None:
        sections.append(
            PromptSection(
                name="model_profile",
                content=build_profile_section(settings),
                order=5,
                cacheable=True,
            )
        )

    if settings.tool_profile != "full":
        sections.append(
            PromptSection(
                name="tool_discovery",
                content=(
                    "# Tool Discovery\n"
                    "Only the core tools are loaded initially. If a task needs a tool "
                    "that is not currently listed, call `tool_search(query=\"...\")` with "
                    "a name or description fragment. Matching tools are enabled for "
                    "subsequent turns; call them normally after searching. This includes "
                    "images, notebooks, LSP, cron/remote jobs, background tasks, "
                    "worktrees, teams, config, brief, and sleep tools."
                ),
                order=25,
                cacheable=True,
            )
        )

    if not coordinator:
        skills_section = _build_skills_section(
            cwd,
            extra_skill_dirs=extra_skill_dirs,
            extra_plugin_roots=extra_plugin_roots,
            settings=settings,
        )
        if skills_section:
            sections.append(
                PromptSection(
                    name="skills",
                    content=skills_section,
                    order=30,
                    cacheable=True,
                )
            )

        delegation = _build_delegation_section(tool_names)
        if delegation:
            sections.append(
                PromptSection(
                    name="delegation",
                    content=delegation,
                    order=40,
                    cacheable=True,
                )
            )

    # Only always-apply rules belong in the stable prefix.  Glob-scoped rules
    # depend on the turn's active files and move to the turn context.
    project_rules, _ = load_project_rules_prompt_parts(cwd, active_files=None)
    if project_rules:
        sections.append(
            PromptSection(
                name="project_rules",
                content=project_rules,
                order=50,
                cacheable=True,
                max_chars=30_000,
            )
        )

    local_rules = load_local_rules()
    if local_rules:
        sections.append(
            PromptSection(
                name="local_rules",
                content=f"# Local Environment Rules\n\n{local_rules}",
                order=55,
                cacheable=True,
                max_chars=12_000,
            )
        )

    return sections, live_environment


def _turn_context_sections(
    settings: Settings,
    *,
    cwd: str | Path,
    latest_user_prompt: str | None,
    coordinator: bool,
    live_environment: str,
    include_project_memory: bool,
    active_files: Iterable[str] | None,
) -> list[PromptSection]:
    """Build the per-turn context that is attached to the latest user message."""
    sections: list[PromptSection] = [
        PromptSection(
            name="permission_mode",
            content=_build_permission_mode_section(settings),
            order=10,
            cacheable=False,
        )
    ]

    if settings.fast_mode:
        sections.append(
            PromptSection(
                name="session_mode",
                content=(
                    "# Session Mode\nFast mode is enabled. Prefer concise replies, "
                    "minimal tool use, and quicker progress over exhaustive exploration."
                ),
                order=15,
                cacheable=False,
            )
        )

    sections.append(
        PromptSection(
            name="reasoning",
            content=(
                "# Reasoning Settings\n"
                f"- Effort: {settings.effort}\n"
                f"- Passes: {settings.passes}\n"
                "Adjust depth and iteration count to match these settings while still completing the task."
            ),
            order=20,
            cacheable=False,
        )
    )

    for name, title, path in (
        ("issue_context", "Issue Context", get_project_issue_file(cwd)),
        ("pr_comments", "Pull Request Comments", get_project_pr_comments_file(cwd)),
        ("active_repo", "Active Repo Context", get_project_active_repo_context_path(cwd)),
    ):
        if path.exists():
            content = path.read_text(encoding="utf-8", errors="replace").strip()
            if content:
                sections.append(
                    PromptSection(
                        name=name,
                        content=f"# {title}\n\n```md\n{content}\n```",
                        order=30,
                        max_chars=12_000,
                    )
                )

    if include_project_memory and settings.memory.enabled:
        memory_section = load_memory_prompt(
            cwd,
            max_entrypoint_lines=settings.memory.max_entrypoint_lines,
            max_entrypoint_bytes=settings.memory.max_entrypoint_bytes,
        )
        if memory_section:
            sections.append(
                PromptSection(
                    name="memory",
                    content=memory_section,
                    order=50,
                    max_chars=20_000,
                )
            )

        if latest_user_prompt:
            relevant = select_relevant_memories(
                latest_user_prompt,
                cwd,
                max_results=settings.memory.max_files,
            )
            if relevant:
                try:
                    headers = [item.header for item in relevant]
                    mark_memory_used(cwd, headers, memory_dir=headers[0].path.parent)
                except OSError:
                    pass
                sections.append(
                    PromptSection(
                        name="relevant_memory",
                        content=format_relevant_memories(relevant),
                        order=55,
                        max_chars=12_000,
                    )
                )

    _, scoped_project_rules = load_project_rules_prompt_parts(
        cwd, active_files=active_files
    )
    if scoped_project_rules:
        sections.append(
            PromptSection(
                name="project_rules_scoped",
                content=scoped_project_rules,
                order=60,
                cacheable=False,
                max_chars=30_000,
            )
        )

    if live_environment:
        sections.append(
            PromptSection(
                name="environment_live",
                content=live_environment,
                order=70,
                cacheable=False,
            )
        )

    return sections


def build_stable_system_prompt_result(
    settings: Settings,
    *,
    cwd: str | Path,
    extra_skill_dirs: Iterable[str | Path] | None = None,
    extra_plugin_roots: Iterable[str | Path] | None = None,
    available_tools: Iterable[str] | None = None,
    max_chars: int = DEFAULT_MAX_PROMPT_CHARS,
) -> PromptBuildResult:
    """Build the byte-stable part of the system prompt.

    This result must not depend on the current user prompt, active files,
    permission mode, effort, fast mode, memory selection, or live environment.
    The runtime builds it once and sends the same bytes for the whole session.
    """
    tool_names = set(available_tools) if available_tools is not None else None
    coordinator = is_coordinator_mode()
    sections, _ = _stable_sections(
        settings,
        cwd=cwd,
        tool_names=tool_names,
        coordinator=coordinator,
        extra_skill_dirs=extra_skill_dirs,
        extra_plugin_roots=extra_plugin_roots,
    )
    return PromptComposer(max_total_chars=max_chars).compose(sections)


def build_turn_context_result(
    settings: Settings,
    *,
    cwd: str | Path,
    latest_user_prompt: str | None = None,
    include_project_memory: bool = True,
    active_files: Iterable[str] | None = None,
    max_chars: int = DEFAULT_MAX_PROMPT_CHARS,
) -> PromptBuildResult:
    """Build the per-turn dynamic context, attached after the user message."""
    coordinator = is_coordinator_mode()
    if coordinator:
        live_environment = ""
    else:
        parts = build_system_prompt_parts(
            custom_prompt=settings.system_prompt,
            cwd=str(cwd),
        )
        live_environment = parts.live
    sections = _turn_context_sections(
        settings,
        cwd=cwd,
        latest_user_prompt=latest_user_prompt,
        coordinator=coordinator,
        live_environment=live_environment,
        include_project_memory=include_project_memory,
        active_files=active_files,
    )
    return PromptComposer(max_total_chars=max_chars).compose(sections)


def build_turn_context(
    settings: Settings,
    *,
    cwd: str | Path,
    latest_user_prompt: str | None = None,
    include_project_memory: bool = True,
    active_files: Iterable[str] | None = None,
    max_chars: int = DEFAULT_MAX_PROMPT_CHARS,
) -> str:
    """Return the per-turn context as plain text."""
    return build_turn_context_result(
        settings,
        cwd=cwd,
        latest_user_prompt=latest_user_prompt,
        include_project_memory=include_project_memory,
        active_files=active_files,
        max_chars=max_chars,
    ).text


def build_runtime_system_prompt_result(
    settings: Settings,
    *,
    cwd: str | Path,
    latest_user_prompt: str | None = None,
    extra_skill_dirs: Iterable[str | Path] | None = None,
    extra_plugin_roots: Iterable[str | Path] | None = None,
    include_project_memory: bool = True,
    available_tools: Iterable[str] | None = None,
    active_files: Iterable[str] | None = None,
    max_chars: int = DEFAULT_MAX_PROMPT_CHARS,
) -> PromptBuildResult:
    """Build the full prompt (stable + turn context) for compatibility.

    Runtime sessions use :func:`build_stable_system_prompt_result` for the
    system message and :func:`build_turn_context` for the latest user message,
    which keeps the system+tools prefix frozen for the whole session.
    """
    stable = build_stable_system_prompt_result(
        settings,
        cwd=cwd,
        extra_skill_dirs=extra_skill_dirs,
        extra_plugin_roots=extra_plugin_roots,
        available_tools=available_tools,
        max_chars=max_chars,
    )
    turn = build_turn_context_result(
        settings,
        cwd=cwd,
        latest_user_prompt=latest_user_prompt,
        include_project_memory=include_project_memory,
        active_files=active_files,
        max_chars=max_chars,
    )
    text = "\n\n".join(part for part in (stable.text, turn.text) if part)
    return PromptBuildResult(
        text=text,
        sections=[*stable.sections, *turn.sections],
        cacheable_prefix=stable.cacheable_prefix,
        version=stable.version,
        section_hashes=stable.section_hashes + turn.section_hashes,
    )


def build_runtime_system_prompt(
    settings: Settings,
    *,
    cwd: str | Path,
    latest_user_prompt: str | None = None,
    extra_skill_dirs: Iterable[str | Path] | None = None,
    extra_plugin_roots: Iterable[str | Path] | None = None,
    include_project_memory: bool = True,
    available_tools: Iterable[str] | None = None,
    active_files: Iterable[str] | None = None,
    max_chars: int = DEFAULT_MAX_PROMPT_CHARS,
) -> str:
    """Build the runtime system prompt with project instructions and memory."""
    return build_runtime_system_prompt_result(
        settings,
        cwd=cwd,
        latest_user_prompt=latest_user_prompt,
        extra_skill_dirs=extra_skill_dirs,
        extra_plugin_roots=extra_plugin_roots,
        include_project_memory=include_project_memory,
        available_tools=available_tools,
        active_files=active_files,
        max_chars=max_chars,
    ).text
