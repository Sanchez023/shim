"""Turn scheduling and mid-turn request handling for the React backend host.

Extracted from :mod:`openharness.ui.backend_host` so that module stays under the
repository's per-file size budget.  The mixin assumes the concrete host provides
the attributes and helpers declared at the top.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Coroutine
from typing import Any

from openharness.domain.messages import ConversationMessage
from openharness.tasks.manager import get_task_manager
from openharness.ui.protocol import (
    BackendEvent,
    FrontendImageAttachment,
    FrontendRequest,
    TranscriptItem,
)

log = logging.getLogger(__name__)


class TurnLifecycleMixin:
    """Start turns as their own task, and serve requests while one is running.

    A turn used to be awaited inline by the request loop, which froze every
    other request (Ctrl+B, busy-safe slash commands, queued follow-ups) until
    the turn finished.  Everything here exists to keep that loop responsive.
    """

    # Provided by the concrete host.
    _bundle: Any
    _busy: bool
    _active_request_task: asyncio.Task[Any] | None
    _request_queue: asyncio.Queue[FrontendRequest]

    async def _emit(self, event: BackendEvent) -> None:  # pragma: no cover - stub
        raise NotImplementedError

    async def _process_line(
        self,
        line: str,
        *,
        transcript_line: str | None = None,
        images: list[FrontendImageAttachment] | None = None,
        end_of_turn: bool = True,
        echo: bool = True,
    ) -> bool:  # pragma: no cover - stub
        raise NotImplementedError

    def _status_snapshot(self) -> BackendEvent:  # pragma: no cover - stub
        raise NotImplementedError

    def _is_busy_safe_line(self, line: str) -> bool:
        """Return True when *line* is a slash command that may run mid-turn."""
        bundle = self._bundle
        if bundle is None or not line.startswith("/"):
            return False
        parsed = bundle.commands.lookup(line)
        return parsed is not None and parsed[0].busy_safe

    async def _run_busy_safe_line(self, line: str) -> None:
        """Run a read-only slash command without disturbing the running turn.

        The turn keeps ownership of ``_busy``; suppressing ``line_complete``
        stops the frontend from clearing its spinner before the turn ends.
        """
        assert self._bundle is not None
        try:
            await self._process_line(line, end_of_turn=False)
        except Exception:
            log.exception("busy-safe command failed: %s", line)
            await self._emit(
                BackendEvent(
                    type="error",
                    message=f"{line.split()[0]} failed while a turn was running",
                )
            )

    async def _queue_message_during_turn(self, line: str) -> None:
        """Queue a follow-up prompt so it lands at the next step boundary."""
        assert self._bundle is not None
        from openharness.ui.file_mentions import expand_file_mentions

        engine = self._bundle.engine
        queue_user_message = getattr(engine, "queue_user_message", None)
        model_line, attached_files = expand_file_mentions(line, self._bundle.cwd)
        queued = False
        if queue_user_message is not None:
            queued = bool(queue_user_message(ConversationMessage.from_user_text(model_line)))
        if not queued:
            await self._emit(BackendEvent(type="error", message="Session is busy"))
            return
        display_text = line
        if attached_files:
            display_text += (
                f"\n[{len(attached_files)} file(s) attached: "
                f"{', '.join(attached_files[:5])}"
                f"{', ...' if len(attached_files) > 5 else ''}]"
            )
        await self._emit(
            BackendEvent(
                type="transcript_item",
                item=TranscriptItem(role="user", text=display_text),
            )
        )
        await self._emit(
            BackendEvent(
                type="transcript_item",
                item=TranscriptItem(
                    role="system",
                    text="Queued - this message is sent at the next tool boundary.",
                ),
            )
        )

    def _start_detached(self, coroutine: Coroutine[Any, Any, Any]) -> None:
        """Run a request as its own task so the loop keeps serving input."""
        task = asyncio.create_task(self._run_detached(coroutine))
        self._active_request_task = task

    async def _run_detached(self, coroutine: Coroutine[Any, Any, Any]) -> None:
        try:
            await coroutine
        except Exception as exc:
            log.exception("request failed")
            await self._emit(BackendEvent(type="error", message=f"Request failed: {exc}"))
            await self._emit(BackendEvent(type="line_complete"))
        finally:
            self._busy = False
            if self._active_request_task is asyncio.current_task():
                self._active_request_task = None

    def _start_turn(
        self,
        line: str,
        images: list[FrontendImageAttachment] | None = None,
        *,
        echo: bool = True,
    ) -> None:
        """Run one turn as its own task.

        The request loop must stay responsive while a turn streams: that is
        what makes Ctrl+B, busy-safe commands and mid-turn follow-ups work.
        """
        message_start = len(self._bundle.engine.messages) if self._bundle is not None else 0
        task = asyncio.create_task(
            self._run_turn(line, images, message_start, time.monotonic(), echo)
        )
        self._active_request_task = task

    async def _run_turn(
        self,
        line: str,
        images: list[FrontendImageAttachment] | None,
        message_start: int,
        turn_started: float,
        echo: bool = True,
    ) -> bool:
        should_continue = True
        try:
            should_continue = await self._run_active_request(
                self._process_line(line, images=images, echo=echo)
            )
        except Exception as exc:
            # A failing turn must not take the whole session (and its queue of
            # follow-ups) down with it.
            log.exception("turn failed")
            await self._emit(BackendEvent(type="error", message=f"Turn failed: {exc}"))
        finally:
            self._busy = False
            self._notify_turn_complete(message_start, time.monotonic() - turn_started)
            if self._active_request_task is asyncio.current_task():
                self._active_request_task = None
        if not should_continue:
            # `/exit` and friends: wake the request loop so it can shut down.
            await self._request_queue.put(FrontendRequest(type="shutdown"))
        else:
            # Follow-ups that arrived after the last step never got injected;
            # give them their own turn instead of stranding them until the next
            # unrelated prompt.
            self._start_pending_followup_turn()
        return should_continue

    def _start_pending_followup_turn(self) -> bool:
        if self._bundle is None:
            return False
        take_pending = getattr(self._bundle.engine, "take_pending_user_messages", None)
        if take_pending is None:
            return False
        texts = [
            message.text.strip()
            for message in take_pending()
            if message.text.strip()
        ]
        if not texts:
            return False
        # The transcript already echoed each of these when they were queued.
        self._busy = True
        self._start_turn("\n\n".join(texts), echo=False)
        return True

    async def _set_ui_preference(self, key: str, value: str) -> None:
        """Persist a purely visual preference (Ctrl+T and friends).

        Nothing here reaches the provider payload, so it is safe while a turn
        is streaming and cannot cost a prompt-cache miss.
        """
        from openharness.config.settings import load_settings, save_settings
        from openharness.state.app_state import TODO_PANEL_MODES

        if self._bundle is None:
            return
        if key == "todo_panel":
            if value not in TODO_PANEL_MODES:
                await self._emit(
                    BackendEvent(type="error", message=f"Unknown todo panel: {value}")
                )
                return
            settings = load_settings()
            settings.todo_panel = value
            save_settings(settings)
            self._bundle.app_state.set(todo_panel=value)
            await self._emit(self._status_snapshot())
            return
        await self._emit(BackendEvent(type="error", message=f"Unknown UI preference: {key}"))

    async def _run_active_request(self, awaitable: Coroutine[Any, Any, bool]) -> bool:
        task = asyncio.create_task(awaitable)
        self._active_request_task = task
        try:
            return await task
        except asyncio.CancelledError:
            await self._emit(
                BackendEvent(
                    type="transcript_item",
                    item=TranscriptItem(role="system", text="Interrupted by user."),
                )
            )
            await self._emit(self._status_snapshot())
            await self._emit(BackendEvent.tasks_snapshot(get_task_manager().list_tasks()))
            await self._emit(BackendEvent(type="line_complete"))
            return True
        finally:
            if self._active_request_task is task:
                self._active_request_task = None

    async def _interrupt_active_request(self) -> None:
        task = self._active_request_task
        if task is None or task.done():
            return
        task.cancel()

    async def _background_active_tool(self) -> None:
        """Ctrl+B: move the running tool call into the background task manager."""
        engine = self._bundle.engine if self._bundle is not None else None
        request_background = getattr(engine, "request_background", None)
        if request_background is not None and request_background():
            await self._emit(
                BackendEvent(
                    type="transcript_item",
                    item=TranscriptItem(
                        role="system",
                        text="Moving the running command to the background...",
                    ),
                )
            )
            return
        await self._emit(
            BackendEvent(
                type="transcript_item",
                item=TranscriptItem(
                    role="system",
                    text="Nothing to background right now (no command is running).",
                ),
            )
        )

