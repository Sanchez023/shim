"""Shared Feishu channel state used by the split mixins."""

from __future__ import annotations

import asyncio
import threading
from collections import OrderedDict
from typing import Any

from openharness.channels.bus.queue import MessageBus
from openharness.channels.impl.feishu_policy import _FeishuSenderInfo
from openharness.config.schema import FeishuConfig


class FeishuBaseMixin:
    """State annotations and the client-initialization contract."""

    name: str
    config: FeishuConfig
    bus: MessageBus
    _client: Any
    _ws_client: Any
    _ws_thread: threading.Thread | None
    _processed_message_ids: OrderedDict[str, None]
    _sender_cache: OrderedDict[str, _FeishuSenderInfo]
    _loop: asyncio.AbstractEventLoop | None

    def _ensure_rest_client(self) -> bool:
        raise NotImplementedError
