"""Byte-level digest gate for the provider request payload (ADR-0008).

Every refactor in this repository must be cache-neutral: the bytes we put in
front of the provider decide whether its prefix cache hits.  The invariants in
``test_cache_stability.py`` check *properties* (stable prefix ignores cwd, tool
activation only appends).  This module pins the *actual payload* instead.

How it works: a recording fake client captures each ``ApiMessageRequest`` and
we fingerprint the cache-relevant parts independently:

* ``system_prompt`` / ``system_prompt_cacheable_prefix`` -- cached verbatim;
* ``tools`` -- order matters, so it is hashed as an ordered JSON array;
* ``messages`` -- order and block order matter, hashed as an ordered array;
* ``model`` / ``max_tokens`` / ``effort`` -- part of the request identity.

``json.dumps`` deliberately runs with ``sort_keys=False``: a dict rebuilt in a
different insertion order is a different payload on the wire, and we want the
gate to notice.

The expected values live in ``tests/fixtures/cache_payload_digest.json``.
Never regenerate them to make a refactor pass -- a diff there means the change
is not cache-neutral.

Run ``python -m tests.test_engine.test_cache_payload_digest --write`` only when
intentionally re-baselining (for example after a deliberate prompt change).
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from openharness.api.client import ApiMessageCompleteEvent
from openharness.api.usage import UsageSnapshot
from openharness.config.settings import PermissionSettings, Settings
from openharness.domain.messages import (
    ConversationMessage,
    TextBlock,
    ToolUseBlock,
)
from openharness.engine.cache_warmup import CacheWarmup
from openharness.engine.query_engine import QueryEngine
from openharness.permissions.checker import PermissionChecker
from openharness.tools.base import BaseTool, ToolRegistry, ToolResult

FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "cache_payload_digest.json"

STABLE_SYSTEM_PROMPT = "STABLE SYSTEM PROMPT\n\nsecond section"
STABLE_PREFIX = "STABLE SYSTEM PROMPT"
SECTION_HASHES = (("base", "0" * 40), ("tools", "1" * 40))


class _EmptyInput(BaseModel):
    pass


class _NamedTool(BaseTool):
    input_model = _EmptyInput

    def __init__(self, name: str) -> None:
        self.name = name
        self.description = f"{name} tool"

    async def execute(self, arguments, context) -> ToolResult:
        del arguments, context
        return ToolResult(output=f"{self.name} output")


class _RecordingClient:
    """Capture each request; answer with one scripted completion."""

    base_url = "https://example.test/v1"

    def __init__(self, responses: list[ConversationMessage] | None = None) -> None:
        self.requests: list[Any] = []
        self._responses = list(responses or [])

    async def stream_message(self, request):
        self.requests.append(_clone_request(request))
        if self._responses:
            message = self._responses.pop(0)
        else:
            message = ConversationMessage(
                role="assistant", content=[TextBlock(text="ok")]
            )
        yield ApiMessageCompleteEvent(
            message=message,
            usage=UsageSnapshot(input_tokens=10, output_tokens=1),
            stop_reason="stop",
        )

    async def warmup(self, request):
        # The real adapters warm the prefix with a non-streaming call; record
        # the same request shape so the digest covers it.
        self.requests.append(_clone_request(request))
        return UsageSnapshot(input_tokens=10, output_tokens=1)


def _clone_request(request):
    """Snapshot a request so later in-place edits cannot rewrite history."""
    import dataclasses

    return dataclasses.replace(
        request,
        messages=[message.model_copy(deep=True) for message in request.messages],
        tools=[dict(schema) for schema in request.tools],
    )


def _registry(*names: str) -> ToolRegistry:
    registry = ToolRegistry()
    for name in names:
        registry.register(_NamedTool(name))
    return registry


def _fixed_turn_context(prompt: str, metadata: dict[str, object]) -> str:
    del prompt, metadata
    return "FIXED TURN CONTEXT"


@pytest.fixture(autouse=True)
def _isolate_coordinator_mode(monkeypatch) -> None:
    """Coordinator tests leave the mode env var behind; the payload must not see it."""
    monkeypatch.delenv("CLAUDE_CODE_COORDINATOR_MODE", raising=False)


def _build_engine(
    cwd: Path,
    client: _RecordingClient,
    *,
    registry: ToolRegistry | None = None,
) -> QueryEngine:
    return QueryEngine(
        api_client=client,
        tool_registry=registry or ToolRegistry(),
        permission_checker=PermissionChecker(PermissionSettings()),
        cwd=cwd,
        model="digest-model",
        system_prompt=STABLE_SYSTEM_PROMPT,
        system_prompt_cacheable_prefix=STABLE_PREFIX,
        system_prompt_section_hashes=SECTION_HASHES,
        settings=Settings(),
        turn_context_builder=_fixed_turn_context,
        warmup_wait_seconds=0.0,
    )


def _drain(iterator) -> None:
    async def _consume() -> None:
        async for _event in iterator:
            pass

    return _consume()


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=False)


def _digest(request) -> dict[str, Any]:
    return {
        "model": request.model,
        "max_tokens": request.max_tokens,
        "effort": request.effort,
        "system_prompt": _sha(request.system_prompt or ""),
        "cacheable_prefix": _sha(request.system_prompt_cacheable_prefix),
        "tools": _sha(_encode(request.tools)),
        "messages": _sha(
            _encode([message.model_dump(mode="json") for message in request.messages])
        ),
    }


async def collect_digests(root: Path) -> dict[str, dict[str, Any]]:
    """Fingerprint every payload shape that touches the provider cache."""
    digests: dict[str, dict[str, Any]] = {}

    # 1. Cold turn, no tools: the baseline system-only prefix.
    cold_root = root / "cold"
    cold_root.mkdir(parents=True, exist_ok=True)
    client = _RecordingClient()
    engine = _build_engine(cold_root, client)
    await _drain(engine.submit_message("hello"))
    digests["cold_no_tools"] = _digest(client.requests[0])

    # 2. Same prompt with three tools: tools are part of the cached prefix.
    tools_root = root / "tools"
    tools_root.mkdir(parents=True, exist_ok=True)
    client = _RecordingClient()
    engine = _build_engine(tools_root, client, registry=_registry("alpha", "beta", "gamma"))
    await _drain(engine.submit_message("hello"))
    digests["with_tools"] = _digest(client.requests[0])

    # 3. Deferred tool activated mid-session: must only append to the tail.
    client = _RecordingClient()
    engine = _build_engine(
        tools_root,
        client,
        registry=_registry("bash", "tool_search", "notebook_edit", "lsp"),
    )
    await _drain(engine.submit_message("first"))
    digests["before_activation"] = _digest(client.requests[-1])
    engine.activate_tools(["notebook_edit"])
    await _drain(engine.submit_message("second"))
    digests["after_activation"] = _digest(client.requests[-1])

    # 4. Cache warmup: a different request shape (messages=[]), same prefix.
    warmup_root = root / "warmup"
    warmup_root.mkdir(parents=True, exist_ok=True)
    client = _RecordingClient()
    engine = _build_engine(warmup_root, client, registry=_registry("alpha", "beta"))
    engine.set_cache_warmup(CacheWarmup())
    await engine.warmup_cache()
    digests["cache_warmup"] = _digest(client.requests[0])

    # 5. continue_pending: no new user message, so the prefix must be reused.
    continue_root = root / "continue"
    continue_root.mkdir(parents=True, exist_ok=True)
    client = _RecordingClient()
    engine = _build_engine(continue_root, client)
    await _drain(engine.submit_message("hello"))
    await _drain(engine.continue_pending())
    digests["continue_pending"] = _digest(client.requests[-1])

    # 6. Tool roundtrip: the assistant tool_use block and the tool_result block
    #    become part of the history we resend.
    roundtrip_root = root / "roundtrip"
    roundtrip_root.mkdir(parents=True, exist_ok=True)
    client = _RecordingClient(
        responses=[
            ConversationMessage(
                role="assistant",
                content=[
                    TextBlock(text="calling the tool"),
                    ToolUseBlock(id="toolu_digest_1", name="echo", input={"value": 1}),
                ],
            ),
            ConversationMessage(role="assistant", content=[TextBlock(text="done")]),
        ]
    )
    engine = _build_engine(roundtrip_root, client, registry=_registry("echo"))
    await _drain(engine.submit_message("use the tool"))
    digests["tool_roundtrip_first"] = _digest(client.requests[0])
    digests["tool_roundtrip_second"] = _digest(client.requests[1])

    return digests


@pytest.mark.asyncio
async def test_provider_payload_digests_are_cache_stable(tmp_path: Path) -> None:
    assert FIXTURE_PATH.exists(), (
        f"missing {FIXTURE_PATH}; create it once at a known-good baseline with "
        "`python -m tests.test_engine.test_cache_payload_digest --write`"
    )
    expected = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    actual = await collect_digests(tmp_path)

    assert actual == expected, (
        "provider payload changed; this is a prompt-cache change, not a refactor. "
        "Diff the digests below instead of regenerating the fixture."
    )


@pytest.mark.asyncio
async def test_activation_only_extends_the_tool_tail(tmp_path: Path) -> None:
    """A tool activation may change the tools segment, never the system prefix."""
    expected = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    before = expected["before_activation"]
    after = expected["after_activation"]

    for key in ("system_prompt", "cacheable_prefix"):
        assert before[key] == after[key], f"{key} changed when a tool was activated"
    assert before["tools"] != after["tools"]


@pytest.mark.asyncio
async def test_every_scenario_shares_the_same_stable_prefix(tmp_path: Path) -> None:
    """Warmup, cold turns, activations and continuations must agree byte-for-byte."""
    expected = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    prefixes = {digest["system_prompt"] for digest in expected.values()}
    cacheable = {digest["cacheable_prefix"] for digest in expected.values()}
    models = {digest["model"] for digest in expected.values()}

    assert len(prefixes) == 1, "system prompt drifted between scenarios"
    assert len(cacheable) == 1, "cacheable prefix drifted between scenarios"
    assert len(models) == 1, "model changed between scenarios"


def _write_fixture() -> None:
    import asyncio
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        digests = asyncio.run(collect_digests(Path(tmp)))
    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE_PATH.write_text(
        json.dumps(digests, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":  # pragma: no cover - maintenance entry point
    if "--write" not in sys.argv:
        raise SystemExit("usage: python -m tests.test_engine.test_cache_payload_digest --write")
    _write_fixture()
