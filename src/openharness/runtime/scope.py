"""Session-scoped registrations with reverse-order disposal."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import ExitStack
from types import TracebackType
from typing import Any, Self

from openharness.runtime.registry import CapabilityRegistry


class SessionScope:
    """Own every registration made on behalf of one session."""

    def __init__(self, *, session_id: str = "") -> None:
        self.session_id = session_id
        self._stack = ExitStack()
        self._closed = False

    def add_disposer(self, disposer: Callable[[], None]) -> None:
        if self._closed:
            disposer()
            return
        self._stack.callback(disposer)

    def register(
        self,
        registry: CapabilityRegistry,
        name: str,
        provider: Any,
        *,
        replace: bool = True,
    ) -> Any:
        """Register a provider and tie its lifetime to this scope."""
        disposer = registry.register(name, provider, replace=replace)
        self.add_disposer(disposer)
        return provider

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stack.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
