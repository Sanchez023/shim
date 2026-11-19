"""Feishu REST transport: message send and managed-group operations."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from openharness.channels.impl.feishu_base import FeishuBaseMixin

logger = logging.getLogger(__name__)


class FeishuApiError(RuntimeError):
    """Raised when a Feishu API call fails."""


class FeishuTransportMixin(FeishuBaseMixin):
    """Message/group REST calls."""

    def _send_message_sync(self, receive_id_type: str, receive_id: str, msg_type: str, content: str, reply_in_thread: str | None = None) -> bool:
        """Send a single message (text/image/file/interactive) synchronously.

        When *reply_in_thread* (a message_id) is provided, the message is
        posted as a reply inside the originating thread via
        ``ReplyMessageRequest`` instead of a standalone message.
        """
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
        try:
            if reply_in_thread:
                from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody
                request = ReplyMessageRequest.builder() \
                    .message_id(reply_in_thread) \
                    .request_body(
                        ReplyMessageRequestBody.builder()
                        .msg_type(msg_type)
                        .content(content)
                        .reply_in_thread(True)
                        .build()
                    ).build()
                response = self._client.im.v1.message.reply(request)
            else:
                request = CreateMessageRequest.builder() \
                    .receive_id_type(receive_id_type) \
                    .request_body(
                        CreateMessageRequestBody.builder()
                        .receive_id(receive_id)
                        .msg_type(msg_type)
                        .content(content)
                        .build()
                    ).build()
                response = self._client.im.v1.message.create(request)
            if not response.success():
                logger.error(
                    "Failed to send Feishu %s message: code=%s, msg=%s, log_id=%s",
                    msg_type, response.code, response.msg, response.get_log_id()
                )
                return False
            logger.debug("Feishu %s message sent to %s", msg_type, receive_id)
            return True
        except Exception as e:
            logger.error("Error sending Feishu %s message: %s", msg_type, e)
            return False

    @staticmethod
    def _format_response_error(action: str, response: Any) -> str:
        log_id = response.get_log_id() if hasattr(response, "get_log_id") else ""
        return (
            f"{action} failed: code={getattr(response, 'code', '')}, "
            f"msg={getattr(response, 'msg', '')}, log_id={log_id}"
        )

    def _create_managed_group_sync(self, user_open_id: str, name: str) -> str:
        """Create a Feishu group containing the user and the app bot."""
        from lark_oapi.api.im.v1 import CreateChatRequest, CreateChatRequestBody

        if not self._client:
            raise RuntimeError("Feishu client not initialized")
        request = (
            CreateChatRequest.builder()
            .user_id_type("open_id")
            .set_bot_manager(True)
            .request_body(
                CreateChatRequestBody.builder()
                .name(name)
                .user_id_list([user_open_id])
                .chat_mode("group")
                .chat_type("private")
                .build()
            )
            .build()
        )
        response = self._client.im.v1.chat.create(request)
        if not response.success():
            raise FeishuApiError(self._format_response_error("create Feishu group", response))
        chat_id = getattr(getattr(response, "data", None), "chat_id", None)
        if not chat_id:
            raise FeishuApiError("create Feishu group failed: response missing chat_id")
        logger.info("Created Feishu managed group name=%r chat_id=%s", name, chat_id)
        return str(chat_id)

    async def create_managed_group(self, *, user_open_id: str, name: str) -> str:
        """Create a Feishu group for a single user and the ohmo bot."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._create_managed_group_sync, user_open_id, name)

    def _rename_group_sync(self, chat_id: str, name: str) -> None:
        """Rename a Feishu group."""
        from lark_oapi.api.im.v1 import UpdateChatRequest, UpdateChatRequestBody

        if not self._client:
            raise RuntimeError("Feishu client not initialized")
        request = (
            UpdateChatRequest.builder()
            .user_id_type("open_id")
            .chat_id(chat_id)
            .request_body(UpdateChatRequestBody.builder().name(name).build())
            .build()
        )
        response = self._client.im.v1.chat.update(request)
        if not response.success():
            raise FeishuApiError(self._format_response_error("rename Feishu group", response))
        logger.info("Renamed Feishu group chat_id=%s name=%r", chat_id, name)

    async def rename_group(self, *, chat_id: str, name: str) -> None:
        """Rename a Feishu group."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._rename_group_sync, chat_id, name)
