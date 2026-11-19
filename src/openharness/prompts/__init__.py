"""System prompt builders and composable sections."""

from openharness.prompts.active_files import (
    collect_active_files,
    matches_globs,
    metadata_has_active_files,
    prompt_mentions_files,
)
from openharness.prompts.claudemd import (
    discover_claude_md_files,
    discover_project_rule_files,
    load_claude_md_prompt,
    load_project_rules_prompt,
)
from openharness.prompts.composer import (
    PromptBuildResult,
    PromptComposer,
    PromptSection,
)
from openharness.prompts.context import (
    build_runtime_system_prompt,
    build_runtime_system_prompt_result,
    build_stable_system_prompt_result,
    build_turn_context,
    build_turn_context_result,
)
from openharness.prompts.environment import get_environment_info
from openharness.prompts.profiles import (
    PromptProfile,
    build_profile_section,
    select_prompt_profile,
)
from openharness.prompts.system_prompt import build_system_prompt

__all__ = [
    "PromptBuildResult",
    "PromptComposer",
    "PromptProfile",
    "PromptSection",
    "build_profile_section",
    "build_runtime_system_prompt",
    "build_runtime_system_prompt_result",
    "build_stable_system_prompt_result",
    "build_system_prompt",
    "build_turn_context",
    "build_turn_context_result",
    "collect_active_files",
    "discover_claude_md_files",
    "discover_project_rule_files",
    "get_environment_info",
    "load_claude_md_prompt",
    "load_project_rules_prompt",
    "matches_globs",
    "metadata_has_active_files",
    "prompt_mentions_files",
    "select_prompt_profile",
]
