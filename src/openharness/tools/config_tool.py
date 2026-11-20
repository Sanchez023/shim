"""Tool for reading and updating settings."""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, Field

from openharness.config.settings import load_settings, save_settings
from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult

# Top-level settings an agent may change.  Everything else is deliberately
# omitted: ``permission``, ``sandbox``, ``hooks``, ``mcp_servers`` and
# ``profiles`` are privilege-escalation and/or code-execution surfaces, and
# ``api_key``/``base_url`` would let one approval redirect credentials.
_WRITABLE_SECTIONS: frozenset[str] = frozenset(
    {
        "effort",
        "fast_mode",
        "notifications",
        "output_style",
        "passes",
        "personality",
        "personality_custom",
        "theme",
        "tool_profile",
        "verbose",
        "vim_mode",
        "voice_mode",
    }
)

# Values under these keys are masked before the settings are shown.
_SECRET_KEY_RE = re.compile(
    r"(?i)(api[_-]?key|token|secret|password|passwd|credential|authorization)"
)


class ConfigToolInput(BaseModel):
    """Arguments for config access."""

    action: str = Field(default="show", description="show or set")
    key: str | None = Field(default=None)
    value: str | None = Field(default=None)


class ConfigTool(BaseTool[ConfigToolInput]):
    """Read or update OpenHarness settings."""

    name = "config"
    description = "Read or update OpenHarness settings."
    input_model = ConfigToolInput

    async def execute(self, arguments: ConfigToolInput, context: ToolExecutionContext) -> ToolResult:
        del context
        settings = load_settings()
        if arguments.action == "show":
            redacted = _redact(settings.model_dump(mode="json"))
            return ToolResult(output=json.dumps(redacted, indent=2, ensure_ascii=False))
        if arguments.action == "set" and arguments.key and arguments.value is not None:
            parts = arguments.key.split(".")
            if parts[0] not in _WRITABLE_SECTIONS:
                return ToolResult(
                    output=(
                        f"Refusing to set {arguments.key!r}: only "
                        f"{', '.join(sorted(_WRITABLE_SECTIONS))} can be changed by "
                        "the agent. Ask the user to change anything else (or run "
                        "`shim config set`)."
                    ),
                    is_error=True,
                )
            target: Any = settings
            for part in parts[:-1]:
                if not hasattr(target, part):
                    return ToolResult(output=f"Unknown config key: {arguments.key}", is_error=True)
                target = getattr(target, part)
            leaf = parts[-1]
            if not hasattr(target, leaf):
                return ToolResult(output=f"Unknown config key: {arguments.key}", is_error=True)
            current = getattr(target, leaf)
            value: Any = arguments.value
            if isinstance(current, bool):
                value = arguments.value.strip().lower() in {"1", "true", "yes", "on"}
            elif isinstance(current, int) and not isinstance(current, bool):
                value = int(arguments.value)
            elif isinstance(current, float):
                value = float(arguments.value)
            setattr(target, leaf, value)
            save_settings(settings)
            return ToolResult(output=f"Updated {arguments.key}")
        return ToolResult(output="Usage: action=show or action=set with key/value", is_error=True)


def _mask(value: str) -> str:
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}…{value[-2:]}"


def _redact(value: Any, key: str = "") -> Any:
    """Recursively mask values stored under secret-looking keys."""
    if isinstance(value, dict):
        return {str(k): _redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(item, key) for item in value]
    if isinstance(value, str) and value and _SECRET_KEY_RE.search(key):
        return _mask(value)
    return value
