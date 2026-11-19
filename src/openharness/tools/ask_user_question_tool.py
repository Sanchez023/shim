"""Tool for asking the interactive user a follow-up question."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable

from pydantic import BaseModel, Field

from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult

AskUserPrompt = Callable[..., Awaitable[str]]


class AskUserQuestionToolInput(BaseModel):
    """Arguments for asking the user a question."""

    question: str = Field(description="The exact question to ask the user")
    options: list[str] = Field(
        default_factory=list,
        description=(
            "Optional choices. When provided, the interactive UI shows a "
            "navigable selection list and returns the chosen option."
        ),
    )


def _accepts_options(prompt: Callable[..., Awaitable[str]]) -> bool:
    """Return whether a prompt callback can receive the options list."""
    try:
        parameters = inspect.signature(prompt).parameters
    except (TypeError, ValueError):
        return True
    if len(parameters) >= 2:
        return True
    return any(
        parameter.kind is inspect.Parameter.VAR_POSITIONAL
        for parameter in parameters.values()
    )


class AskUserQuestionTool(BaseTool):
    """Ask the interactive user a question and return the answer."""

    name = "ask_user_question"
    description = (
        "Ask the interactive user a follow-up question and return the answer. "
        "Use it when requirements are genuinely ambiguous or a decision belongs to the "
        "user instead of guessing, and especially before planning. "
        "Pass options to present a selectable list when there are clear choices; the UI "
        "also offers a custom answer."
    )
    input_model = AskUserQuestionToolInput

    def is_read_only(self, arguments: AskUserQuestionToolInput) -> bool:
        del arguments
        return True

    async def execute(
        self,
        arguments: AskUserQuestionToolInput,
        context: ToolExecutionContext,
    ) -> ToolResult:
        prompt = context.metadata.get("ask_user_prompt")
        if not callable(prompt):
            return ToolResult(
                output="ask_user_question is unavailable in this session",
                is_error=True,
            )
        options = [str(option).strip() for option in arguments.options if str(option).strip()]
        if _accepts_options(prompt):
            answer = str(await prompt(arguments.question, options)).strip()
        else:
            answer = str(await prompt(arguments.question)).strip()
        if not answer:
            return ToolResult(output="(no response)")
        return ToolResult(output=answer)
