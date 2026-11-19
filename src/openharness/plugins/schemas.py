"""Plugin manifest schemas."""

from __future__ import annotations

from pydantic import BaseModel, Field


class PluginVariable(BaseModel):
    """A configurable variable or token declared by a plugin."""

    description: str = ""
    secret: bool = False
    required: bool = False
    env: str | None = None
    default: str | None = None


class PluginManifest(BaseModel):
    """Plugin manifest stored in plugin.json or .claude-plugin/plugin.json."""

    name: str
    version: str = "0.0.0"
    description: str = ""
    enabled_by_default: bool = True
    skills_dir: str = "skills"
    tools_dir: str = "tools"
    hooks_file: str = "hooks.json"
    mcp_file: str = "mcp.json"
    # Extended fields: optional author, commands, agents, variables, etc.
    variables: dict[str, PluginVariable] = Field(default_factory=dict)
    author: dict | None = None
    commands: str | list | dict | None = None
    agents: str | list | None = None
    skills: str | list | None = None
    hooks: str | dict | list | None = None
