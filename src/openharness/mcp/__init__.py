"""MCP exports."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from openharness.mcp.client import McpClientManager, McpServerNotConnectedError
    from openharness.mcp.config import load_mcp_server_configs
    from openharness.mcp.types import (
        McpConnectionStatus,
        McpHttpServerConfig,
        McpJsonConfig,
        McpResourceInfo,
        McpServerConfig,
        McpStdioServerConfig,
        McpToolInfo,
        McpWebSocketServerConfig,
    )

__all__ = [
    "McpClientManager",
    "McpConnectionStatus",
    "McpHttpServerConfig",
    "McpJsonConfig",
    "McpResourceInfo",
    "McpServerConfig",
    "McpServerNotConnectedError",
    "McpStdioServerConfig",
    "McpToolInfo",
    "McpWebSocketServerConfig",
    "load_mcp_server_configs",
]


def __getattr__(name: str) -> Any:
    if name == "McpClientManager":
        from openharness.mcp.client import McpClientManager

        return McpClientManager
    if name == "McpServerNotConnectedError":
        from openharness.mcp.client import McpServerNotConnectedError

        return McpServerNotConnectedError
    if name == "load_mcp_server_configs":
        from openharness.mcp.config import load_mcp_server_configs

        return load_mcp_server_configs
    if name in {
        "McpConnectionStatus",
        "McpHttpServerConfig",
        "McpJsonConfig",
        "McpResourceInfo",
        "McpServerConfig",
        "McpStdioServerConfig",
        "McpToolInfo",
        "McpWebSocketServerConfig",
    }:
        from openharness.mcp.types import (
            McpConnectionStatus,
            McpHttpServerConfig,
            McpJsonConfig,
            McpResourceInfo,
            McpServerConfig,
            McpStdioServerConfig,
            McpToolInfo,
            McpWebSocketServerConfig,
        )

        return {
            "McpConnectionStatus": McpConnectionStatus,
            "McpHttpServerConfig": McpHttpServerConfig,
            "McpJsonConfig": McpJsonConfig,
            "McpResourceInfo": McpResourceInfo,
            "McpServerConfig": McpServerConfig,
            "McpStdioServerConfig": McpStdioServerConfig,
            "McpToolInfo": McpToolInfo,
            "McpWebSocketServerConfig": McpWebSocketServerConfig,
        }[name]
    raise AttributeError(name)
