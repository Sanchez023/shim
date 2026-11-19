"""Import-hygiene gate for the core packages.

Function-scope imports are not free: they hide real coupling, defeat static
analysis, and turn a module-level cycle into a runtime surprise.  After the
``query_types`` leaf landed, the core packages need almost none of them.

Two reasons are still allowed, and every instance must be listed below:

1. **dependency direction** -- the core must not import a feature package
   (``engine -> coordinator``) or two packages that would form a cycle;
2. **cheap package import** -- ``__init__`` re-exports stay lazy so importing a
   package does not drag in the whole implementation.

The allowlist is asserted in both directions: a new lazy import fails the test,
and a stale entry (import since hoisted to module scope) fails it too.
"""

from __future__ import annotations

import ast
from pathlib import Path

CORE_PACKAGES = ("api", "domain", "engine", "prompts", "runtime")

ALLOWED_FUNCTION_IMPORTS: dict[tuple[str, str], str] = {
    ("engine/__init__.py", "openharness.domain.messages"): "cheap package import",
    ("engine/__init__.py", "openharness.domain.stream_events"): "cheap package import",
    ("engine/__init__.py", "openharness.engine.query_engine"): "cheap package import",
    ("engine/query_engine.py", "openharness.coordinator.coordinator_mode"): (
        "dependency direction: core must not import the coordinator at module scope"
    ),
    ("engine/prompt_state.py", "openharness.prompts.active_files"): (
        "cheap import: only needed when a prompt builder is configured"
    ),
    ("api/provider.py", "openharness.api.copilot_auth"): (
        "dependency direction: auth pulls in the api package"
    ),
}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _nested_openharness_imports(path: Path) -> set[tuple[str, str]]:
    """Return ``(module, imported)`` pairs for imports inside function bodies."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.ImportFrom):
                module = child.module or ""
                if module.startswith("openharness"):
                    found.add((module, ""))
            elif isinstance(child, ast.Import):
                for alias in child.names:
                    if alias.name.startswith("openharness"):
                        found.add((alias.name, ""))
    return found


def _scan() -> dict[tuple[str, str], str]:
    src = _repo_root() / "src" / "openharness"
    actual: dict[tuple[str, str], str] = {}
    for package in CORE_PACKAGES:
        for path in sorted((src / package).rglob("*.py")):
            relative = path.relative_to(src).as_posix()
            for module, _ in _nested_openharness_imports(path):
                actual[(relative, module)] = relative
    return actual


def test_core_packages_do_not_grow_function_scope_imports() -> None:
    actual = _scan()
    unexpected = sorted(set(actual) - set(ALLOWED_FUNCTION_IMPORTS))
    assert not unexpected, (
        "new function-scope import in a core package:\n"
        + "\n".join(f"  {path}: {module}" for path, module in unexpected)
        + "\n\nEither hoist it to module scope (the preferred fix) or add it to "
        "ALLOWED_FUNCTION_IMPORTS with the reason in the comment."
    )


def test_import_allowlist_has_no_stale_entries() -> None:
    actual = _scan()
    stale = sorted(set(ALLOWED_FUNCTION_IMPORTS) - set(actual))
    assert not stale, (
        "allowlist entries no longer needed:\n"
        + "\n".join(f"  {path}: {module}" for path, module in stale)
        + "\n\nDelete them; the ratchet only works if it shrinks."
    )
