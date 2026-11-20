"""MCP client manager."""

from __future__ import annotations

import asyncio
import contextlib
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult, ReadResourceResult
from pydantic import AnyUrl

from openharness.domain.messages import ImageBlock
from openharness.mcp.types import (
    McpConnectionStatus,
    McpHttpServerConfig,
    McpResourceInfo,
    McpServerConfig,
    McpStdioServerConfig,
    McpToolInfo,
)


class McpServerNotConnectedError(Exception):
    """Raised when an MCP server is not connected or its session has been lost."""


@dataclass(frozen=True)
class McpToolOutput:
    """One MCP tool call result: text plus any images the server returned."""

    text: str
    images: list[ImageBlock] = field(default_factory=list)

    def __str__(self) -> str:
        """Render as the text part so text-only callers keep working."""
        return self.text


#: How many images one MCP call may forward to the model.  A third-party server
#: that returns a gallery must not be able to flood the prompt.
MAX_IMAGES_PER_RESULT = 4

#: Largest base64 payload forwarded as an image block (~3 MB of binary).  Any
#: bigger screenshot costs more prompt tokens than the picture is worth.
MAX_IMAGE_B64_CHARS = 4_000_000


def _image_block_from_mcp(item: Any, *, count: int) -> ImageBlock | None:
    """Convert one MCP ``ImageContent`` item into an :class:`ImageBlock`."""
    if count >= MAX_IMAGES_PER_RESULT:
        return None
    media_type = str(getattr(item, "mimeType", "") or "").strip()
    payload = getattr(item, "data", None)
    if not media_type.startswith("image/") or not isinstance(payload, str) or not payload:
        return None
    if len(payload) > MAX_IMAGE_B64_CHARS:
        return None
    return ImageBlock(media_type=media_type, data=payload)


#: Upper bound on a single MCP tool call.
MCP_CALL_TIMEOUT_SECONDS = 300.0


class McpClientManager:
    """Manage MCP connections and expose tools/resources."""

    def __init__(self, server_configs: dict[str, McpServerConfig]) -> None:
        self._server_configs = server_configs
        # A tool call that never returns must not wedge the turn; MCP servers
        # are third-party processes and can hang (ADR-0012 D3).
        self._call_timeout_seconds: float = MCP_CALL_TIMEOUT_SECONDS
        self._statuses: dict[str, McpConnectionStatus] = {
            name: McpConnectionStatus(
                name=name,
                state="pending",
                transport=getattr(config, "type", "unknown"),
            )
            for name, config in server_configs.items()
        }
        self._sessions: dict[str, ClientSession] = {}
        self._stacks: dict[str, AsyncExitStack] = {}

    async def connect_all(self) -> None:
        """Connect all configured MCP servers supported by the current build."""
        for name, config in self._server_configs.items():
            if isinstance(config, McpStdioServerConfig):
                await self._connect_stdio(name, config)
            elif isinstance(config, McpHttpServerConfig):
                await self._connect_http(name, config)
            else:
                # Unknown / unsupported config objects still report the
                # transport they declared, if any.
                transport = str(getattr(config, "type", "unknown"))
                self._statuses[name] = McpConnectionStatus(
                    name=name,
                    state="failed",
                    transport=transport,
                    auth_configured=bool(getattr(config, "headers", None)),
                    detail=f"Unsupported MCP transport in current build: {transport}",
                )

    async def reconnect_all(self) -> None:
        """Reconnect all configured servers."""
        await self.close()
        self._statuses = {
            name: McpConnectionStatus(name=name, state="pending", transport=getattr(config, "type", "unknown"))
            for name, config in self._server_configs.items()
        }
        await self.connect_all()

    def update_server_config(self, name: str, config: McpServerConfig) -> None:
        """Replace one server config in memory."""
        self._server_configs[name] = config

    def get_server_config(self, name: str) -> McpServerConfig | None:
        """Return one configured server object if present."""
        return self._server_configs.get(name)

    async def _close_failed_stack(self, stack: AsyncExitStack) -> None:
        """Best-effort cleanup for a connection attempt that never finished."""
        try:
            await stack.aclose()
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise

    def _mark_connection_failed(
        self,
        name: str,
        config: object,
        *,
        auth_configured: bool,
        exc: BaseException,
    ) -> None:
        """Record one MCP connection failure without aborting startup."""
        self._statuses[name] = McpConnectionStatus(
            name=name,
            state="failed",
            transport=getattr(config, "type", "unknown"),
            auth_configured=auth_configured,
            detail=str(exc) or exc.__class__.__name__,
        )

    async def close(self) -> None:
        """Close all active MCP sessions."""
        for stack in list(self._stacks.values()):
            with contextlib.suppress(RuntimeError, asyncio.CancelledError):
                await stack.aclose()
        self._stacks.clear()
        self._sessions.clear()

    def list_statuses(self) -> list[McpConnectionStatus]:
        """Return statuses for all configured servers."""
        return [self._statuses[name] for name in sorted(self._statuses)]

    def list_tools(self) -> list[McpToolInfo]:
        """Return all connected MCP tools."""
        tools: list[McpToolInfo] = []
        for status in self.list_statuses():
            tools.extend(status.tools)
        return tools

    def list_resources(self) -> list[McpResourceInfo]:
        """Return all connected MCP resources."""
        resources: list[McpResourceInfo] = []
        for status in self.list_statuses():
            resources.extend(status.resources)
        return resources

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> McpToolOutput:
        """Invoke one MCP tool, keeping image content as image blocks."""
        session = self._sessions.get(server_name)
        if session is None:
            status = self._statuses.get(server_name)
            detail = status.detail if status else "unknown server"
            raise McpServerNotConnectedError(
                f"MCP server '{server_name}' is not connected: {detail}"
            )
        try:
            async with asyncio.timeout(self._call_timeout_seconds):
                result: CallToolResult = await session.call_tool(tool_name, arguments)
        except TimeoutError as exc:
            # A server that never answers used to hold the turn open forever
            # (ADR-0012 D3).
            raise McpServerNotConnectedError(
                f"MCP server '{server_name}' did not answer "
                f"'{tool_name}' within {self._call_timeout_seconds:.0f}s"
            ) from exc
        except Exception as exc:
            raise McpServerNotConnectedError(
                f"MCP server '{server_name}' call failed: {exc}"
            ) from exc
        parts: list[str] = []
        images: list[ImageBlock] = []
        omitted = 0
        for item in result.content:
            kind = getattr(item, "type", None)
            if kind == "text":
                parts.append(getattr(item, "text", ""))
            elif kind == "image":
                image = _image_block_from_mcp(item, count=len(images))
                if image is None:
                    omitted += 1
                else:
                    images.append(image)
            else:
                parts.append(item.model_dump_json())
        if result.structuredContent and not parts and not images:
            parts.append(str(result.structuredContent))
        if omitted:
            parts.append(f"[{omitted} image(s) omitted: unsupported, oversized or over the per-call limit]")
        if not parts and not images:
            parts.append("(no output)")
        if images and not parts:
            parts.append(f"[{len(images)} image(s) returned]")
        return McpToolOutput(text="\n".join(parts).strip(), images=images)

    async def read_resource(self, server_name: str, uri: str) -> str:
        """Read one MCP resource and stringify the response."""
        session = self._sessions.get(server_name)
        if session is None:
            status = self._statuses.get(server_name)
            detail = status.detail if status else "unknown server"
            raise McpServerNotConnectedError(
                f"MCP server '{server_name}' is not connected: {detail}"
            )
        try:
            result: ReadResourceResult = await session.read_resource(AnyUrl(uri))
        except Exception as exc:
            raise McpServerNotConnectedError(
                f"MCP server '{server_name}' resource read failed: {exc}"
            ) from exc
        parts: list[str] = []
        for item in result.contents:
            text = getattr(item, "text", None)
            if text is not None:
                parts.append(text)
            else:
                parts.append(str(getattr(item, "blob", "")))
        return "\n".join(parts).strip()

    async def _connect_stdio(self, name: str, config: McpStdioServerConfig) -> None:
        stack = AsyncExitStack()
        try:
            read_stream, write_stream = await stack.enter_async_context(
                stdio_client(
                    StdioServerParameters(
                        command=config.command,
                        args=config.args,
                        env=config.env,
                        cwd=config.cwd,
                    )
                )
            )
            await self._register_connected_session(
                name=name,
                config=config,
                stack=stack,
                read_stream=read_stream,
                write_stream=write_stream,
                auth_configured=bool(config.env),
            )
        except asyncio.CancelledError as exc:
            await self._close_failed_stack(stack)
            self._mark_connection_failed(
                name,
                config,
                auth_configured=bool(config.env),
                exc=exc,
            )
        except Exception as exc:
            await self._close_failed_stack(stack)
            self._mark_connection_failed(
                name,
                config,
                auth_configured=bool(config.env),
                exc=exc,
            )

    async def _connect_http(self, name: str, config: McpHttpServerConfig) -> None:
        stack = AsyncExitStack()
        try:
            http_client = await stack.enter_async_context(
                httpx.AsyncClient(headers=config.headers or None)
            )
            read_stream, write_stream, _get_session_id = await stack.enter_async_context(
                streamable_http_client(config.url, http_client=http_client)
            )
            await self._register_connected_session(
                name=name,
                config=config,
                stack=stack,
                read_stream=read_stream,
                write_stream=write_stream,
                auth_configured=bool(config.headers),
            )
        except asyncio.CancelledError as exc:
            await self._close_failed_stack(stack)
            self._mark_connection_failed(
                name,
                config,
                auth_configured=bool(config.headers),
                exc=exc,
            )
        except Exception as exc:
            await self._close_failed_stack(stack)
            self._mark_connection_failed(
                name,
                config,
                auth_configured=bool(config.headers),
                exc=exc,
            )

    async def _register_connected_session(
        self,
        *,
        name: str,
        config: object,
        stack: AsyncExitStack,
        read_stream: Any,
        write_stream: Any,
        auth_configured: bool,
    ) -> None:
        session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
        await session.initialize()
        tool_result = await session.list_tools()
        resource_result = None
        try:
            resource_result = await session.list_resources()
        except Exception as exc:
            if "Method not found" not in str(exc):
                raise
        tools = [
            McpToolInfo(
                server_name=name,
                name=tool.name,
                description=tool.description or "",
                input_schema=dict(tool.inputSchema or {"type": "object", "properties": {}}),
                read_only_hint=bool(
                    getattr(getattr(tool, "annotations", None), "readOnlyHint", False)
                ),
            )
            for tool in tool_result.tools
        ]
        resources = [
            McpResourceInfo(
                server_name=name,
                name=resource.name or str(resource.uri),
                uri=str(resource.uri),
                description=resource.description or "",
            )
            for resource in (resource_result.resources if resource_result is not None else [])
        ]
        self._sessions[name] = session
        self._stacks[name] = stack
        self._statuses[name] = McpConnectionStatus(
            name=name,
            state="connected",
            transport=getattr(config, "type", "unknown"),
            auth_configured=auth_configured,
            tools=tools,
            resources=resources,
        )
