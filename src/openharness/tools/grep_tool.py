"""Content search tool with a pure-Python fallback."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import time
from collections.abc import Iterable, Iterator
from pathlib import Path

from pydantic import BaseModel, Field

from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult


class GrepToolInput(BaseModel):
    """Arguments for the grep tool."""

    pattern: str = Field(description="Regular expression to search for")
    root: str | None = Field(
        default=None,
        description="Search root directory or file. For multiple roots, call grep separately per root.",
    )
    file_glob: str = Field(default="**/*")
    case_sensitive: bool = Field(default=True)
    limit: int = Field(default=200, ge=1, le=2000)
    timeout_seconds: int = Field(default=20, ge=1, le=120)


class GrepTool(BaseTool):
    """Search text files for a regex pattern."""

    name = "grep"
    description = "Search file contents with a regular expression."
    input_model = GrepToolInput

    def is_read_only(self, arguments: GrepToolInput) -> bool:
        del arguments
        return True

    async def execute(self, arguments: GrepToolInput, context: ToolExecutionContext) -> ToolResult:
        root = _resolve_path(context.cwd, arguments.root) if arguments.root else context.cwd
        if not root.exists():
            return ToolResult(
                output=(
                    f"Search root does not exist: {root}\n"
                    "If you intended multiple roots, call grep separately for each root."
                ),
                is_error=True,
            )
        if root.is_file():
            display_base = _display_base(root, context.cwd)
            matches = await _rg_grep_file(
                path=root,
                pattern=arguments.pattern,
                case_sensitive=arguments.case_sensitive,
                limit=arguments.limit,
                display_base=display_base,
                timeout_seconds=arguments.timeout_seconds,
            )
            if matches is not None:
                return _format_rg_result(matches, arguments.timeout_seconds)

            return await _python_grep_result(
                paths=[root],
                pattern=arguments.pattern,
                case_sensitive=arguments.case_sensitive,
                limit=arguments.limit,
                display_base=display_base,
                timeout_seconds=arguments.timeout_seconds,
            )

        # Prefer ripgrep for performance; fallback to Python when unavailable.
        matches = await _rg_grep(
            root=root,
            pattern=arguments.pattern,
            file_glob=arguments.file_glob,
            case_sensitive=arguments.case_sensitive,
            limit=arguments.limit,
            timeout_seconds=arguments.timeout_seconds,
        )
        if matches is not None:
            return _format_rg_result(matches, arguments.timeout_seconds)

        # Python fallback (kept for portability). Walking a workspace takes far
        # longer than a regex match, so it runs in a worker thread and honours
        # ``timeout_seconds``: blocking the event loop here freezes the UI and
        # makes the turn uninterruptible.
        return await _python_grep_result(
            paths=_iter_search_files(root, arguments.file_glob),
            pattern=arguments.pattern,
            case_sensitive=arguments.case_sensitive,
            limit=arguments.limit,
            display_base=root,
            timeout_seconds=arguments.timeout_seconds,
        )


def _display_base(path: Path, cwd: Path) -> Path:
    try:
        path.relative_to(cwd)
    except ValueError:
        return path.parent
    return cwd


# Vendored and cache trees dominate a repo-wide walk and almost never hold the
# answer. The fallback has no `.gitignore` support, so without this list a
# `grep` over a project root spends minutes in `.venv/` and `.git/`.
_EXCLUDED_DIR_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "env",
        "virtualenv",
        "node_modules",
        "__pycache__",
        "site-packages",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ".tox",
        ".nox",
        ".eggs",
        "dist",
        "build",
        ".next",
        ".nuxt",
        ".cache",
        "target",
    }
)

_RECURSIVE_GLOBS = frozenset({"**/*", "**", "*"})


def _iter_search_files(root: Path, file_glob: str) -> Iterator[Path]:
    """Yield candidate files, pruning vendored trees for tree-wide globs."""
    if file_glob in _RECURSIVE_GLOBS:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [name for name in dirnames if name not in _EXCLUDED_DIR_NAMES]
            for filename in filenames:
                yield Path(dirpath) / filename
        return
    for path in root.glob(file_glob):
        if any(part in _EXCLUDED_DIR_NAMES for part in path.parts):
            continue
        yield path


_BINARY_SAMPLE_BYTES = 4_096
_DEADLINE_CHECK_EVERY_LINES = 256


def _looks_binary(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return b"\x00" in handle.read(_BINARY_SAMPLE_BYTES)
    except OSError:
        return True


def _python_grep_files(
    *,
    paths: Iterable[Path],
    pattern: str,
    case_sensitive: bool,
    limit: int,
    display_base: Path,
    deadline: float | None = None,
) -> tuple[str, bool]:
    """Search files synchronously and return ``(output, timed_out)``.

    Blocking by design: callers run this in a worker thread via
    :func:`_python_grep_result`. Files are streamed line by line so a single
    huge file cannot blow the memory budget or overshoot the deadline by an
    unbounded amount.
    """
    # Python fallback (kept for portability).
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        compiled = re.compile(pattern, flags)
    except re.error as exc:
        return f"(invalid regex pattern '{pattern}': {exc})", False
    collected: list[str] = []

    for path in paths:
        if len(collected) >= limit:
            break
        if deadline is not None and time.monotonic() > deadline:
            return _render_python_matches(collected), True
        if not path.is_file() or _looks_binary(path):
            continue
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line_no, line in enumerate(handle, start=1):
                    if (
                        deadline is not None
                        and line_no % _DEADLINE_CHECK_EVERY_LINES == 0
                        and time.monotonic() > deadline
                    ):
                        return _render_python_matches(collected), True
                    if compiled.search(line):
                        match_text = line.rstrip("\n").rstrip("\r")
                        collected.append(
                            f"{_format_path(path, display_base)}:{line_no}:{match_text}"
                        )
                        if len(collected) >= limit:
                            break
        except OSError:
            continue

    return _render_python_matches(collected), False


def _render_python_matches(collected: list[str]) -> str:
    return "\n".join(collected) if collected else "(no matches)"


async def _python_grep_result(
    *,
    paths: Iterable[Path],
    pattern: str,
    case_sensitive: bool,
    limit: int,
    display_base: Path,
    timeout_seconds: int,
) -> ToolResult:
    """Run the Python fallback off the event loop and enforce its time limit."""
    deadline = time.monotonic() + timeout_seconds
    try:
        output, timed_out = await asyncio.wait_for(
            asyncio.to_thread(
                _python_grep_files,
                paths=paths,
                pattern=pattern,
                case_sensitive=case_sensitive,
                limit=limit,
                display_base=display_base,
                deadline=deadline,
            ),
            # The worker checks the deadline between files and every few
            # hundred lines; this outer guard covers a blocking read (network
            # mount, pathological file) so the turn can never hang.
            timeout=timeout_seconds + 1.0,
        )
    except TimeoutError:
        output, timed_out = "(no matches)", True
    if not timed_out:
        return ToolResult(output=output)
    notice = f"[grep timed out after {timeout_seconds} seconds]"
    return ToolResult(
        output=f"{output}\n\n{notice}" if output != "(no matches)" else notice,
        is_error=True,
    )


def _resolve_path(base: Path, candidate: str | None) -> Path:
    path = Path(candidate or ".").expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _format_rg_result(matches: list[str], timeout_seconds: int) -> ToolResult:
    timed_out = bool(matches and matches[-1] == _timeout_marker(timeout_seconds))
    rendered = matches[:-1] if timed_out else matches
    output = "\n".join(rendered) if rendered else "(no matches)"
    if timed_out:
        output = (
            f"{output}\n\n[grep timed out after {timeout_seconds} seconds]"
            if output != "(no matches)"
            else f"[grep timed out after {timeout_seconds} seconds]"
        )
    return ToolResult(output=output, is_error=timed_out)


async def _rg_grep(
    *,
    root: Path,
    pattern: str,
    file_glob: str,
    case_sensitive: bool,
    limit: int,
    timeout_seconds: int,
) -> list[str] | None:
    """Return matches using ripgrep, or None if ripgrep is unavailable."""
    rg = shutil.which("rg")
    if not rg:
        return None

    include_hidden = (root / ".git").exists() or (root / ".gitignore").exists()
    cmd: list[str] = [
        rg,
        "--no-heading",
        "--line-number",
        "--color",
        "never",
    ]
    if include_hidden:
        cmd.append("--hidden")
    if not case_sensitive:
        cmd.append("-i")
    if file_glob:
        cmd.extend(["--glob", file_glob])
    # `--` ensures patterns like `-foo` aren't parsed as flags.
    cmd.extend(["--", pattern, "."])

    from openharness.sandbox.session import get_docker_sandbox

    session = get_docker_sandbox()
    if session is not None and session.is_running:
        process = await session.exec_command(
            cmd,
            cwd=root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    else:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=8 * 1024 * 1024,  # 8 MB per line — avoids LimitOverrunError on long lines
        )

    matches: list[str] = []
    try:
        await asyncio.wait_for(
            _collect_rg_matches(process, matches, limit=limit),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        matches.append(_timeout_marker(timeout_seconds))
        await _terminate_process(process)
    except asyncio.CancelledError:
        await _terminate_process(process)
        raise
    finally:
        if len(matches) >= limit and process.returncode is None:
            await _terminate_process(process)
        elif process.returncode is None:
            await process.wait()

    # rg exits 0 when matches are found, 1 when none are found.
    # Any other return code indicates an error; fall back to Python.
    if process.returncode in {0, 1, -15, -9}:
        return matches
    return None


async def _rg_grep_file(
    *,
    path: Path,
    pattern: str,
    case_sensitive: bool,
    limit: int,
    display_base: Path,
    timeout_seconds: int,
) -> list[str] | None:
    rg = shutil.which("rg")
    if not rg:
        return None

    cmd: list[str] = [
        rg,
        "--no-heading",
        "--line-number",
        "--color",
        "never",
    ]
    if not case_sensitive:
        cmd.append("-i")
    cmd.extend(["--", pattern, path.name])

    from openharness.sandbox.session import get_docker_sandbox

    session = get_docker_sandbox()
    if session is not None and session.is_running:
        process = await session.exec_command(
            cmd,
            cwd=path.parent,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    else:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(path.parent),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=8 * 1024 * 1024,  # 8 MB per line — avoids LimitOverrunError on long lines
        )

    matches: list[str] = []
    try:
        await asyncio.wait_for(
            _collect_rg_file_matches(
                process,
                matches,
                limit=limit,
                path=path,
                display_base=display_base,
            ),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        matches.append(_timeout_marker(timeout_seconds))
        await _terminate_process(process)
    except asyncio.CancelledError:
        await _terminate_process(process)
        raise
    finally:
        if len(matches) >= limit and process.returncode is None:
            await _terminate_process(process)
        elif process.returncode is None:
            await process.wait()

    if process.returncode in {0, 1, -15, -9}:
        return matches
    return None


def _timeout_marker(timeout_seconds: int) -> str:
    return f"__OPENHARNESS_GREP_TIMEOUT__:{timeout_seconds}"


async def _collect_rg_matches(
    process: asyncio.subprocess.Process,
    matches: list[str],
    *,
    limit: int,
) -> None:
    assert process.stdout is not None
    while len(matches) < limit:
        try:
            raw = await process.stdout.readline()
        except ValueError:
            # Line exceeded the stream buffer limit; skip it and continue.
            continue
        if not raw:
            break
        line = raw.decode("utf-8", errors="replace").rstrip("\n")
        if line:
            matches.append(line)


async def _collect_rg_file_matches(
    process: asyncio.subprocess.Process,
    matches: list[str],
    *,
    limit: int,
    path: Path,
    display_base: Path,
) -> None:
    assert process.stdout is not None
    while len(matches) < limit:
        try:
            raw = await process.stdout.readline()
        except ValueError:
            # Line exceeded the stream buffer limit; skip it and continue.
            continue
        if not raw:
            break
        line = raw.decode("utf-8", errors="replace").rstrip("\n")
        if not line:
            continue
        matches.append(f"{_format_path(path, display_base)}:{line}")


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=2.0)
    except TimeoutError:
        process.kill()
        await process.wait()
    return


def _format_path(path: Path, display_base: Path) -> str:
    try:
        return str(path.relative_to(display_base))
    except ValueError:
        return str(path)
