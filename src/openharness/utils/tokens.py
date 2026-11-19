"""Simple token estimation utilities."""

from __future__ import annotations

# CJK ideographs, kana, hangul and CJK punctuation tokenize much more densely
# than Latin text: roughly one token per character instead of one per four.
# Treating them as ASCII silently halves the estimate for Chinese/Japanese/
# Korean conversations, which makes compaction fire far too late.
_CJK_RANGES: tuple[tuple[int, int], ...] = (
    (0x3000, 0x303F),  # CJK symbols and punctuation
    (0x3040, 0x30FF),  # Hiragana and Katakana
    (0x3400, 0x4DBF),  # CJK Unified Ideographs Extension A
    (0x4E00, 0x9FFF),  # CJK Unified Ideographs
    (0xAC00, 0xD7AF),  # Hangul syllables
    (0xF900, 0xFAFF),  # CJK compatibility ideographs
    (0xFF00, 0xFFEF),  # Halfwidth and fullwidth forms
)


def _is_cjk(character: str) -> bool:
    code = ord(character)
    return any(start <= code <= end for start, end in _CJK_RANGES)


def estimate_tokens(text: str) -> int:
    """Estimate tokens from text, counting CJK characters individually."""
    if not text:
        return 0
    cjk = sum(1 for character in text if _is_cjk(character))
    other = len(text) - cjk
    return max(1, cjk + (other + 3) // 4)


def estimate_message_tokens(messages: list[str]) -> int:
    """Estimate tokens for a collection of message strings."""
    return sum(estimate_tokens(message) for message in messages)
