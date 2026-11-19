"""Track whether a session's edits have been verified since the last green run.

The engine already records *exploration*: :meth:`TurnState.remember_verified_work`
collects "Inspected file X" and "Ran bash ..." into a list that is later rendered
as ``## Verified Work`` in the session-memory hand-off.  Nothing distinguished
that from a verification that actually re-exercised the code after the last
edit, so a turn that edited five files and never ran the suite still finished
with ``status="completed"`` and a hand-off note claiming the work was verified.

This module is the missing state.  It is deliberately narrow:

* It only observes.  Every decision lives in :func:`status`, and every effect
  (tool-result suffix, completion gate, hand-off block) is decided by the
  caller, so the behaviour stays greppable from the call sites.
* Values stored in ``tool_metadata`` are plain ``dict``/``list`` because that
  dict is serialized into session snapshots; the dataclasses here are the read
  view only.
* Commands it cannot classify are ignored rather than guessed at.  A false
  "verified" is worse than a missed one -- the gate exists to stop exactly
  that class of mistake.

Two counters instead of one, because not every write invalidates a test run.
``edit_seq`` counts edits the tools can see (``edit_file``/``write_file``) and
anything that makes a verification stale by definition.  ``opaque_seq`` counts
shell commands that are not confidently read-only -- except VCS bookkeeping
(``git add``/``commit``/``stash``) and package installs, which cannot turn a
green suite stale.  Counting those would fire the gate on every commit and
train the reader to ignore it.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

#: Key inside ``tool_metadata``.  Must also be listed in
#: :mod:`openharness.services.session_storage` or the ledger is dropped on
#: resume and after every compaction.
LEDGER_KEY = "verification_ledger"

#: Config passed in by the assembly layer (see ``Settings.verification``).
CONFIG_KEY = "verification_gate"

MAX_TRACKED_EDITS = 24
MAX_TRACKED_RUNS = 8
MAX_COMMAND_CHARS = 160

#: Tools that write file content the ledger can see.
EDIT_TOOL_NAMES = frozenset({"edit_file", "write_file", "notebook_edit"})

KIND_TEST = "test"
KIND_LINT = "lint"
KIND_TYPECHECK = "typecheck"
KIND_BUILD = "build"

SCOPE_FULL = "full"
SCOPE_PARTIAL = "partial"

_CODE_SUFFIXES = (
    ".py",
    ".pyi",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".rs",
    ".go",
    ".java",
    ".kt",
    ".rb",
    ".php",
    ".c",
    ".cc",
    ".cpp",
    ".h",
    ".hpp",
    ".cs",
    ".swift",
    ".scala",
    ".ex",
    ".exs",
    ".sql",
)
_CODE_DIRS = ("src/", "tests/", "test/", "scripts/", "lib/", "app/", "pkg/", "internal/")

# Ordered: the first match wins, so ``npm run build`` must be checked before the
# generic test pattern never sees it (it does not match "test" anyway, but
# "ruff format" must not be mistaken for "ruff check").
_KIND_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        KIND_BUILD,
        re.compile(
            r"\b(?:npm|pnpm|yarn)\s+(?:run\s+)?build\b|\bcargo\s+build\b|\bgo\s+build\b"
            r"|\bmake\s+build\b|\bwebpack\b|\bvite\s+build\b"
        ),
    ),
    (
        KIND_TYPECHECK,
        re.compile(r"\bmypy\b|\bpyright\b|\bvue-tsc\b|\btsc\b"),
    ),
    (
        KIND_LINT,
        re.compile(r"\bruff\b(?!\s+format\b)|\beslint\b|\bflake8\b|\bpylint\b|\bbiome\s+(?:check|lint)\b"),
    ),
    (
        KIND_TEST,
        re.compile(
            r"\bpytest\b|\bpy\.test\b|\btox\b|\bnox\b"
            r"|\b(?:npm|pnpm|yarn)\s+(?:run\s+)?test\b|\bmake\s+(?:test|check|verify)\b"
        ),
    ),
)

#: Flags that narrow a run to a subset of the suite.
_PARTIAL_FLAGS = ("-k", "--lf", "--last-failed", "--deselect", "-m", "--ignore")

_SEGMENT_SPLIT = re.compile(r"&&|\|\||;|\n|\|")

#: Shell verbs that are not confidently read-only but never make a green suite
#: stale either: they move files around the index or install dependencies.
_BOOKKEEPING_PREFIXES = (
    "git add",
    "git commit",
    "git stash",
    "git tag",
    "git branch",
    "git checkout",
    "git switch",
    "git restore",
    "git reset",
    "git push",
    "git fetch",
    "git pull",
    "git merge",
    "git rebase",
    "git remote",
    "git config",
    "git worktree",
    "git init",
    "git mv",
    "git rm",
    "git clean",
    "npm install",
    "npm i ",
    "npm ci",
    "npm run install",
    "pnpm install",
    "pnpm i ",
    "yarn install",
    "yarn add",
    "pip install",
    "uv pip install",
    "uv sync",
    "uv lock",
    "poetry install",
    "cargo fetch",
    "go mod",
    "apt-get",
    "apt ",
    "brew ",
)


@dataclass(frozen=True)
class VerificationRun:
    """One classified verification command and the workspace it ran against."""

    kind: str
    command: str
    returncode: int
    scope: str
    edit_seq: int
    opaque_seq: int
    at: float

    @property
    def passed(self) -> bool:
        return self.returncode == 0

    @property
    def closes_gate(self) -> bool:
        """Only a green, workspace-wide run can close the gate."""

        return self.passed and self.scope == SCOPE_FULL


@dataclass(frozen=True)
class GateStatus:
    """What the completion gate should do with this session right now."""

    needs_verification: bool
    reason: str
    last_green: VerificationRun | None
    edits_since_green: int
    nudge_allowed: bool

    def __bool__(self) -> bool:
        return self.needs_verification


def _state(metadata: MutableMapping[str, object] | None) -> dict[str, Any] | None:
    """Return the mutable ledger, creating it on first use.

    ``None`` means the caller has no session metadata at all (unit tests and
    one-shot CLI paths); every observer then becomes a no-op instead of
    silently attaching state to a dict nobody persists.
    """

    if metadata is None:
        return None
    raw = metadata.get(LEDGER_KEY)
    if not isinstance(raw, dict):
        raw = {}
        metadata[LEDGER_KEY] = raw
    ledger: dict[str, Any] = raw
    ledger.setdefault("edit_seq", 0)
    ledger.setdefault("opaque_seq", 0)
    ledger.setdefault("edited", [])
    ledger.setdefault("runs", [])
    ledger.setdefault("nudges_since_green", 0)
    ledger.setdefault("armed", False)
    return ledger


def config(metadata: Mapping[str, object] | None) -> dict[str, object]:
    """Return the gate configuration the assembly layer injected."""

    if metadata is None:
        return {}
    raw = metadata.get(CONFIG_KEY)
    return raw if isinstance(raw, dict) else {}


def _int_option(options: Mapping[str, object], key: str, default: int) -> int:
    value = options.get(key)
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    return default


def _bool_option(options: Mapping[str, object], key: str, default: bool) -> bool:
    value = options.get(key)
    return value if isinstance(value, bool) else default


def is_code_path(path: str) -> bool:
    """Whether editing *path* can invalidate a test run.

    Documentation, data and media edits do not arm the gate: nagging after a
    ``docs/`` change is how a gate gets turned off.
    """

    normalized = path.replace("\\", "/").lstrip("./")
    if not normalized:
        return False
    if normalized.endswith(_CODE_SUFFIXES):
        return True
    return any(f"/{directory}" in f"/{normalized}" for directory in _CODE_DIRS)


def classify_command(command: str) -> tuple[str, str] | None:
    """Return ``(kind, scope)`` for a verification command, else ``None``.

    Scope is ``partial`` when the command narrows the run with ``-k``, a marker
    filter, a single file or a single test id.  A partial run is still recorded
    -- it is evidence -- but it never closes the gate.
    """

    kind: str | None = None
    for candidate, pattern in _KIND_PATTERNS:
        if pattern.search(command):
            kind = candidate
            break
    if kind is None:
        return None
    return kind, _scope_for(command, kind)


def _scope_for(command: str, kind: str) -> str:
    for segment in _SEGMENT_SPLIT.split(command):
        tokens = _split_tokens(segment)
        if not tokens or tokens[0] == "cd":
            # ``cd frontend/terminal && npm run build`` is the only way to run
            # that build; the directory argument is not a narrowed target.
            continue
        match_index = _kind_token_index(tokens, kind)
        if match_index is None:
            continue
        tail = tokens[match_index + 1 :]
        if any(token in _PARTIAL_FLAGS for token in tail):
            return SCOPE_PARTIAL
        if kind in {KIND_TEST, KIND_LINT, KIND_TYPECHECK} and _has_path_argument(tail):
            return SCOPE_PARTIAL
    return SCOPE_FULL


def _split_tokens(segment: str) -> list[str]:
    return [token for token in re.split(r"\s+", segment.strip()) if token]


def _kind_token_index(tokens: list[str], kind: str) -> int | None:
    """Index of the token that starts the verification command.

    Needed to tell ``python -m pytest`` (the ``-m`` belongs to the interpreter)
    from ``pytest -m slow`` (the ``-m`` filters the run).
    """

    for pattern_kind, pattern in _KIND_PATTERNS:
        if pattern_kind != kind:
            continue
        for index, token in enumerate(tokens):
            if pattern.search(token):
                return index
    return None


def _has_path_argument(tokens: list[str]) -> bool:
    """Whether an argument looks like a specific file or directory target.

    Bare directory names such as ``tests`` or ``src`` are the project-wide
    default and stay "full"; ``tests/test_api`` and ``src/foo.py`` are partial.
    """

    for token in tokens:
        if token.startswith("-"):
            continue
        cleaned = token.strip("'\"")
        if not cleaned or cleaned in {".", "tests", "test", "src", "scripts"}:
            continue
        path = PurePosixPath(cleaned.replace("\\", "/"))
        if "/" in cleaned or path.suffix in _CODE_SUFFIXES:
            # ``-q`` style flags are handled above; a token that still contains
            # a slash is a target, unless it names a nested project directory
            # the command must run in.
            return True
    return False


def _is_bookkeeping(command: str) -> bool:
    lowered = command.strip().lower()
    return any(lowered == prefix.strip() or lowered.startswith(prefix) for prefix in _BOOKKEEPING_PREFIXES)


def _looks_read_only(
    command: str,
    read_only: Callable[[str], bool] | None,
) -> bool:
    """Ask the caller's classifier whether this shell command only inspects.

    The predicate is injected rather than imported: ``permissions`` reaches the
    whole tool layer, and the engine is not allowed to depend on it (ADR-0010).
    With no predicate the ledger assumes read-only -- a missed invalidation is
    cheaper than firing the gate on every ``mkdir``.
    """

    if read_only is None:
        return True
    try:
        return read_only(command)
    except (OSError, ValueError):  # pragma: no cover - never gate on a crash
        return True


def record_edit(metadata: MutableMapping[str, object] | None, path: str | None) -> None:
    """Note an edit the tools could see, invalidating earlier verifications.

    An unknown path arms the gate: the caller is an edit tool, so the write
    happened even when the path could not be resolved. Being nagged once is a
    cheaper failure than reporting an unverified edit as done.
    """

    ledger = _state(metadata)
    if ledger is None:
        return
    ledger["edit_seq"] = int(ledger.get("edit_seq", 0)) + 1
    if path is None or is_code_path(path):
        ledger["armed"] = True
    edited = ledger.get("edited")
    if not isinstance(edited, list):
        edited = []
        ledger["edited"] = edited
    if path:
        if path in edited:
            edited.remove(path)
        edited.append(path)
        del edited[:-MAX_TRACKED_EDITS]


def _record_opaque_write(ledger: dict[str, Any]) -> None:
    ledger["opaque_seq"] = int(ledger.get("opaque_seq", 0)) + 1


def record_run(
    metadata: MutableMapping[str, object] | None,
    *,
    command: str,
    returncode: int,
) -> VerificationRun | None:
    """Record a verification run, or return ``None`` when it is not one."""

    classified = classify_command(command)
    if classified is None:
        return None
    ledger = _state(metadata)
    if ledger is None:
        return None
    kind, scope = classified
    run = VerificationRun(
        kind=kind,
        command=command.strip()[:MAX_COMMAND_CHARS],
        returncode=returncode,
        scope=scope,
        edit_seq=int(ledger.get("edit_seq", 0)),
        opaque_seq=int(ledger.get("opaque_seq", 0)),
        at=time.time(),
    )
    runs = ledger.get("runs")
    if not isinstance(runs, list):
        runs = []
        ledger["runs"] = runs
    runs.append(
        {
            "kind": run.kind,
            "command": run.command,
            "returncode": run.returncode,
            "scope": run.scope,
            "edit_seq": run.edit_seq,
            "opaque_seq": run.opaque_seq,
            "at": run.at,
        }
    )
    del runs[:-MAX_TRACKED_RUNS]
    if run.closes_gate:
        ledger["nudges_since_green"] = 0
    return run


def record_tool_call(
    metadata: MutableMapping[str, object] | None,
    *,
    tool_name: str,
    tool_input: Mapping[str, object] | None,
    is_error: bool,
    resolved_file_path: str | None,
    result_metadata: Mapping[str, object] | None = None,
    read_only: Callable[[str], bool] | None = None,
) -> None:
    """Update the ledger from one finished tool call.

    Called from the dispatcher for every tool, so it must stay allocation-light
    and must never raise: a bookkeeping bug here would break unrelated tools.
    """

    ledger = _state(metadata)
    if ledger is None:
        return
    inputs: Mapping[str, object] = tool_input or {}

    if tool_name in EDIT_TOOL_NAMES:
        if is_error:
            return
        record_edit(metadata, resolved_file_path or _first_string(inputs, ("path", "file_path", "notebook_path")))
        return

    if tool_name != "bash":
        return

    command = _string_input(inputs, "command")
    if not command:
        return
    returncode = _returncode(result_metadata)
    if returncode is None:
        return
    # A failed command still carries evidence ("the suite is red"), so record
    # the run before deciding whether it also moved the workspace.
    run = record_run(metadata, command=command, returncode=returncode)
    if run is not None:
        if _fixes_in_place(command):
            # ``ruff check --fix`` rewrote files: the run cannot close the gate
            # for the tree it just changed.
            record_edit(metadata, None)
        return
    if _looks_read_only(command, read_only):
        return
    if _is_bookkeeping(command):
        return
    _record_opaque_write(ledger)


def _fixes_in_place(command: str) -> bool:
    """Whether a verification command also rewrites source files."""

    return bool(re.search(r"\s--fix\b|\s--write\b", command))


def _string_input(inputs: Mapping[str, object], key: str) -> str:
    value = inputs.get(key)
    return value if isinstance(value, str) else ""


def _first_string(inputs: Mapping[str, object], keys: tuple[str, ...]) -> str:
    """First non-empty string among *keys*.

    Edit tools disagree on the parameter name (``path`` for the built-ins,
    ``file_path`` in some provider-shaped schemas), and a miss here would leave
    the gate unarmed exactly when an edit landed.
    """

    for key in keys:
        value = _string_input(inputs, key)
        if value:
            return value
    return ""


def _returncode(result_metadata: Mapping[str, object] | None) -> int | None:
    if not result_metadata:
        return None
    value = result_metadata.get("returncode")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if result_metadata.get("timed_out"):
        return None
    return value


def runs(metadata: Mapping[str, object] | None) -> tuple[VerificationRun, ...]:
    """Read the recorded runs, newest last."""

    if metadata is None:
        return ()
    ledger = metadata.get(LEDGER_KEY)
    if not isinstance(ledger, dict):
        return ()
    raw = ledger.get("runs")
    if not isinstance(raw, list):
        return ()
    parsed: list[VerificationRun] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        kind = entry.get("kind")
        command = entry.get("command")
        returncode = entry.get("returncode")
        scope = entry.get("scope")
        edit_seq = entry.get("edit_seq")
        opaque_seq = entry.get("opaque_seq")
        at = entry.get("at")
        if not isinstance(kind, str) or not isinstance(command, str) or not isinstance(scope, str):
            continue
        if not isinstance(returncode, int) or isinstance(returncode, bool):
            continue
        parsed.append(
            VerificationRun(
                kind=kind,
                command=command,
                returncode=returncode,
                scope=scope,
                edit_seq=edit_seq if isinstance(edit_seq, int) else 0,
                opaque_seq=opaque_seq if isinstance(opaque_seq, int) else 0,
                at=at if isinstance(at, float) else 0.0,
            )
        )
    return tuple(parsed)


def last_green_run(metadata: Mapping[str, object] | None) -> VerificationRun | None:
    for run in reversed(runs(metadata)):
        if run.closes_gate:
            return run
    return None


def status(
    metadata: Mapping[str, object] | None,
    *,
    nudge_limit: int = 1,
) -> GateStatus:
    """Decide whether this session's edits are unverified.

    Closed means: a workspace-wide green run exists and nothing the ledger can
    see has moved since.  Anything else is open -- including "a full run
    happened but failed", which is the case that used to read as success.
    """

    options = config(metadata)
    if not _bool_option(options, "enabled", True):
        return GateStatus(False, "", last_green_run(metadata), 0, False)

    ledger = metadata.get(LEDGER_KEY) if metadata is not None else None
    if not isinstance(ledger, dict) or not ledger.get("armed"):
        return GateStatus(False, "", last_green_run(metadata), 0, False)

    require_full = _bool_option(options, "require_full_suite", True)
    edit_seq = int(ledger.get("edit_seq", 0))
    opaque_seq = int(ledger.get("opaque_seq", 0))
    nudges = int(ledger.get("nudges_since_green", 0))
    green = last_green_run(metadata)
    allowed = nudges < max(nudge_limit, 0)

    if green is None:
        reason = "no workspace-wide run this session"
        return GateStatus(True, reason, None, edit_seq, allowed)

    closed = green.edit_seq == edit_seq and (not require_full or green.opaque_seq == opaque_seq)
    if closed:
        return GateStatus(False, "", green, 0, allowed)

    removed = edit_seq - green.edit_seq
    if removed <= 0:
        reason = f"{opaque_seq - green.opaque_seq} untracked shell writes since the last green run"
    else:
        reason = f"{removed} edit(s) since the last green run ({_short_command(green.command)})"
    return GateStatus(True, reason, green, max(removed, 0), allowed)


def _short_command(command: str) -> str:
    return command if len(command) <= 60 else command[:57] + "..."


def note_nudge(metadata: MutableMapping[str, object] | None) -> None:
    """Count a completion nudge so the gate cannot loop on one turn."""

    ledger = _state(metadata)
    if ledger is None:
        return
    ledger["nudges_since_green"] = int(ledger.get("nudges_since_green", 0)) + 1


def nudge_limit_from(metadata: Mapping[str, object] | None) -> int:
    return max(_int_option(config(metadata), "nudge_limit", 1), 0)


def hygiene_enabled(metadata: Mapping[str, object] | None) -> bool:
    return _bool_option(config(metadata), "hygiene_check", True)


def nudge_budget_left(metadata: Mapping[str, object] | None, limit: int | None = None) -> bool:
    """Whether a reminder may still be injected for this unverified streak.

    Kept separate from :class:`GateStatus` because a blocking STOP hook also
    spends this budget while the verification gate itself may be closed.
    """

    if limit is None:
        limit = nudge_limit_from(metadata)
    if limit <= 0:
        return False
    ledger = metadata.get(LEDGER_KEY) if metadata is not None else None
    if not isinstance(ledger, dict):
        return True
    return int(ledger.get("nudges_since_green", 0)) < limit


def tool_status_line(metadata: Mapping[str, object] | None) -> str:
    """One line appended to a tool result, or ``""`` when there is nothing to say.

    A tool result is the last thing the model reliably reads back
    (``TodoWriteTool`` learned this the hard way -- see ``_open_items_suffix``),
    so this is the only channel that changes behaviour mid-turn.
    """

    gate = status(metadata, nudge_limit=nudge_limit_from(metadata))
    if not gate.needs_verification:
        return ""
    green = gate.last_green
    if green is None:
        return "[verification] no workspace-wide run has been recorded this session."
    return f"[verification] stale: {gate.reason}."


def completion_nudge(gate: GateStatus) -> str:
    """The synthetic user message injected once when a turn ends unverified."""

    green = gate.last_green
    known = (
        f"The last green run was `{_short_command(green.command)}`."
        if green is not None
        else "No workspace-wide run has been recorded this session."
    )
    return (
        "Completion gate: you presented this work as done, but it is not verified "
        f"({gate.reason}). {known}\n"
        "Do one of two things, then stop:\n"
        "1. Run the verification that covers what you changed and report the result.\n"
        "2. State in one line why this change needs no verification "
        "(docs-only, no code path touched, verification impossible here)."
    )


def stop_block_nudge(reason: str) -> str:
    """The synthetic user message injected when a configured STOP hook blocked."""

    detail = reason.strip() or "the hook reported a failure"
    return (
        f"A stop hook blocked this turn: {detail}\n"
        "Resolve it, or state in one line why the hook does not apply here, then stop."
    )


__all__ = [
    "CONFIG_KEY",
    "EDIT_TOOL_NAMES",
    "LEDGER_KEY",
    "GateStatus",
    "VerificationRun",
    "classify_command",
    "completion_nudge",
    "config",
    "hygiene_enabled",
    "is_code_path",
    "last_green_run",
    "note_nudge",
    "nudge_budget_left",
    "nudge_limit_from",
    "record_edit",
    "record_run",
    "record_tool_call",
    "runs",
    "status",
    "stop_block_nudge",
    "tool_status_line",
]
