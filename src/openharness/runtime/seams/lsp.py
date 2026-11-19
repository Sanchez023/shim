"""Code-intelligence port used by the model-facing LSP tool."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class LspLocation:
    """Provider-neutral symbol location."""

    name: str
    kind: str
    path: Path
    line: int
    character: int
    signature: str = ""
    docstring: str = ""


@dataclass(frozen=True)
class LspReference:
    """Provider-neutral reference location."""

    path: Path
    line: int
    text: str = ""


@runtime_checkable
class LspPort(Protocol):
    """Python code-intelligence operations."""

    def document_symbols(self, path: Path) -> list[LspLocation]:
        """Return symbols in one file."""

    def workspace_symbols(self, root: Path, query: str) -> list[LspLocation]:
        """Return symbols whose names contain *query*."""

    def definition(
        self,
        *,
        root: Path,
        file_path: Path,
        symbol: str | None = None,
        line: int | None = None,
        character: int | None = None,
    ) -> list[LspLocation]:
        """Resolve candidate definitions."""

    def references(
        self,
        *,
        root: Path,
        file_path: Path,
        symbol: str | None = None,
        line: int | None = None,
        character: int | None = None,
    ) -> list[LspReference]:
        """Return line-oriented references."""

    def hover(
        self,
        *,
        root: Path,
        file_path: Path,
        symbol: str | None = None,
        line: int | None = None,
        character: int | None = None,
    ) -> LspLocation | None:
        """Return the best hover target."""
