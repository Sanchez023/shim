"""Service-backed implementation of the LSP port."""

from __future__ import annotations

from pathlib import Path

from openharness.runtime.seams.lsp import LspLocation, LspReference
from openharness.services.lsp import (
    find_references,
    go_to_definition,
    hover,
    list_document_symbols,
    workspace_symbol_search,
)


def _location(symbol) -> LspLocation:
    return LspLocation(
        name=symbol.name,
        kind=symbol.kind,
        path=symbol.path,
        line=symbol.line,
        character=symbol.character,
        signature=symbol.signature,
        docstring=symbol.docstring,
    )


class ServiceLspPort:
    """Delegate code-intelligence operations to the Python service."""

    def document_symbols(self, path: Path) -> list[LspLocation]:
        return [_location(symbol) for symbol in list_document_symbols(path)]

    def workspace_symbols(self, root: Path, query: str) -> list[LspLocation]:
        return [_location(symbol) for symbol in workspace_symbol_search(root, query)]

    def definition(
        self,
        *,
        root: Path,
        file_path: Path,
        symbol: str | None = None,
        line: int | None = None,
        character: int | None = None,
    ) -> list[LspLocation]:
        return [
            _location(item)
            for item in go_to_definition(
                root=root,
                file_path=file_path,
                symbol=symbol,
                line=line,
                character=character,
            )
        ]

    def references(
        self,
        *,
        root: Path,
        file_path: Path,
        symbol: str | None = None,
        line: int | None = None,
        character: int | None = None,
    ) -> list[LspReference]:
        return [
            LspReference(path=path, line=ref_line, text=text)
            for path, ref_line, text in find_references(
                root=root,
                file_path=file_path,
                symbol=symbol,
                line=line,
                character=character,
            )
        ]

    def hover(
        self,
        *,
        root: Path,
        file_path: Path,
        symbol: str | None = None,
        line: int | None = None,
        character: int | None = None,
    ) -> LspLocation | None:
        result = hover(
            root=root,
            file_path=file_path,
            symbol=symbol,
            line=line,
            character=character,
        )
        return _location(result) if result is not None else None
