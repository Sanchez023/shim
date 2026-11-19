"""Feishu mention, sender and group-policy helpers."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any

from openharness.config.schema import FeishuConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _FeishuSenderInfo:
    open_id: str
    display_name: str


def _clean_mention_value(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _normalize_mention_name(value: str) -> str:
    return value.strip().lstrip("@").strip().lower()


def _configured_bot_names(config: FeishuConfig) -> set[str]:
    raw_names = getattr(config, "bot_names", []) or []
    if isinstance(raw_names, str):
        items = [item.strip() for item in raw_names.split(",")]
    else:
        items = [str(item).strip() for item in raw_names]
    names = {_normalize_mention_name(item) for item in items if item.strip()}
    return names or {"ohmo"}


def _mention_value(raw: Any, key: str) -> Any:
    if isinstance(raw, dict):
        return raw.get(key)
    return getattr(raw, key, None)


def _mention_id_value(raw_id: Any, key: str) -> Any:
    if isinstance(raw_id, dict):
        return raw_id.get(key)
    return getattr(raw_id, key, None)


def _extract_feishu_mentions(
    content_json: dict,
    raw_mentions: list[Any] | tuple[Any, ...] | None = None,
) -> list[dict[str, str]]:
    """Return normalized mention metadata from Feishu text/post payloads."""
    mentions: list[dict[str, str]] = []
    seen: set[tuple[str, str, str, str, str]] = set()

    def add(raw: Any) -> None:
        mention_id = _mention_value(raw, "id") or {}
        record = {
            "key": _clean_mention_value(_mention_value(raw, "key")),
            "name": _clean_mention_value(_mention_value(raw, "name") or _mention_value(raw, "user_name")),
            "open_id": _clean_mention_value(
                _mention_value(raw, "open_id") or _mention_id_value(mention_id, "open_id")
            ),
            "user_id": _clean_mention_value(
                _mention_value(raw, "user_id") or _mention_id_value(mention_id, "user_id")
            ),
            "union_id": _clean_mention_value(
                _mention_value(raw, "union_id") or _mention_id_value(mention_id, "union_id")
            ),
        }
        key = (record["key"], record["name"], record["open_id"], record["user_id"], record["union_id"])
        if any(record.values()) and key not in seen:
            seen.add(key)
            mentions.append(record)

    if raw_mentions:
        for item in raw_mentions:
            add(item)

    raw_mentions = content_json.get("mentions")
    if isinstance(raw_mentions, list):
        for item in raw_mentions:
            if isinstance(item, dict):
                add(item)

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if value.get("tag") == "at":
                add(value)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(content_json)
    return mentions


def _feishu_mentions_bot(
    content_json: dict,
    text: str,
    config: FeishuConfig,
    *,
    mentions: list[dict[str, str]] | None = None,
) -> bool:
    """Best-effort bot mention detection for Feishu group messages."""
    bot_open_id = str(getattr(config, "bot_open_id", "") or "").strip()
    bot_names = _configured_bot_names(config)
    for mention in mentions if mentions is not None else _extract_feishu_mentions(content_json):
        ids = {mention.get("open_id", ""), mention.get("user_id", ""), mention.get("union_id", "")}
        if bot_open_id and bot_open_id in ids:
            return True
        name = _normalize_mention_name(mention.get("name", ""))
        if name and any(name == candidate or candidate in name for candidate in bot_names):
            return True

    normalized_text = text.strip().lower()
    for name in bot_names:
        if re.search(rf"(^|\s)@{re.escape(name)}(?=$|\s|[:：,，])", normalized_text):
            return True
    return False


def _normalize_feishu_group_policy(value: str | None) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "all": "open",
        "always": "open",
        "always_reply": "open",
        "managed_mention": "managed_or_mention",
        "managed_or_at": "managed_or_mention",
        "at": "mention",
        "mentions": "mention",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized in {"open", "mention", "managed", "managed_or_mention"}:
        return normalized
    return "managed_or_mention"


def _is_ohmo_managed_feishu_group(chat_id: str) -> bool:
    workspace = os.environ.get("OHMO_WORKSPACE")
    if not workspace:
        return False
    try:
        from ohmo.group_registry import load_managed_group_record

        return load_managed_group_record(
            workspace=workspace,
            channel="feishu",
            chat_id=chat_id,
        ) is not None
    except Exception:
        logger.exception("Failed to load ohmo managed Feishu group metadata chat_id=%s", chat_id)
        return False


def _should_process_feishu_group_message(
    *,
    chat_type: str,
    chat_id: str,
    mentions_bot: bool,
    config: FeishuConfig,
) -> bool:
    if str(chat_type or "").strip().lower() != "group":
        return True
    policy = _normalize_feishu_group_policy(getattr(config, "group_policy", "managed_or_mention"))
    if policy == "open":
        return True
    if policy == "mention":
        return mentions_bot
    if policy == "managed":
        return _is_ohmo_managed_feishu_group(chat_id)
    if policy == "managed_or_mention":
        return mentions_bot or _is_ohmo_managed_feishu_group(chat_id)
    return mentions_bot
