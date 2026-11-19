"""Backward-compatible re-exports for token estimation.

The implementation lives in :mod:`openharness.utils.tokens`.
"""

from __future__ import annotations

from openharness.utils.tokens import estimate_message_tokens, estimate_tokens

__all__ = ["estimate_message_tokens", "estimate_tokens"]
