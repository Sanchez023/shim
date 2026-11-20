"""Classify shell commands as read-only for permission decisions.

The bash tool cannot be marked read-only at the tool level because the same
tool runs ``git status`` and ``rm -rf /``.  In default permission mode every
bash call therefore asks the user for confirmation, which makes even a plain
read-only command interrupt the session.

This module gives the permission checker a *conservative* classifier: a command
counts as read-only only when every pipeline segment uses an allowlisted
command with an allowlisted argument shape, contains no redirection, command
substitution, or background job, and does not reference a credential path.

Anything the classifier does not positively recognise returns ``False``, so
unknown commands still prompt.  This is an optimisation for obvious read-only
commands, not a sandbox.
"""

from __future__ import annotations

import fnmatch
import glob as glob_module
import itertools
import os
import re
import shlex
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from openharness.permissions.checker import SENSITIVE_PATH_PATTERNS

# Trees that contain credential material.  A read-only command may inspect a
# file inside one of these trees only after the user approves it, so any
# candidate path that *is* one of these trees, sits inside one, or contains one
# (for recursive tools such as ``grep -r``) disqualifies the command.
_SENSITIVE_ROOTS: tuple[str, ...] = (
    "~/.ssh",
    "~/.aws",
    "~/.config/gcloud",
    "~/.azure",
    "~/.gnupg",
    "~/.docker",
    "~/.kube",
    # Only the credential-bearing parts of the harness data dir are trees.
    # Session transcripts, raw user pastes and logs can contain whatever the
    # session saw.  The rest of ~/.openharness (skills, plugins, themes, tool
    # artifacts) is harness data the agent is expected to read; the actual key
    # files under it are covered by SENSITIVE_PATH_PATTERNS.
    "~/.openharness/sessions",
    "~/.openharness/data/sessions",
    "~/.openharness/data/user_pastes",
    "~/.openharness/logs",
)

# Commands that only read.  Commands with any possibility of writing or of
# running another program (sed, awk, xargs, tee, env, python -c, ...) are
# deliberately absent.
_SAFE_COMMANDS: frozenset[str] = frozenset(
    {
        "[",
        "basename",
        "cat",
        "cd",
        "cmp",
        "column",
        "comm",
        "cut",
        "date",
        "df",
        "diff",
        "dirname",
        "du",
        "echo",
        "egrep",
        "expr",
        "false",
        "fd",
        "fdfind",
        "fgrep",
        "file",
        "find",
        "fmt",
        "fold",
        "grep",
        "head",
        "hostname",
        "id",
        "join",
        "jq",
        "ls",
        "md5sum",
        "nl",
        "paste",
        "printf",
        "pwd",
        "readlink",
        "realpath",
        "rg",
        "seq",
        "sha1sum",
        "sha256sum",
        "sha512sum",
        "sleep",
        "sort",
        "stat",
        "tac",
        "tail",
        "test",
        "tree",
        "tr",
        "true",
        "type",
        "uniq",
        "uname",
        "uptime",
        "wc",
        "where",
        "whereis",
        "which",
        "whoami",
    }
)

_FIND_DANGEROUS_PREFIXES: tuple[str, ...] = (
    "-delete",
    "-exec",
    "-execdir",
    "-ok",
    "-okdir",
    "-fprint",
    "-fprintf",
    "-fls",
)

# Commands that write the workspace or execute arbitrary code by nature.  Seeing
# one is a *positive* answer ("this mutates"), which is what lets the classifier
# tell ``rm -rf build`` apart from ``xargs wc -l``: the second one cannot be
# proven read-only, but it is not evidence of a write either (ADR-0013 D1).
_KNOWN_MUTATING_COMMANDS: frozenset[str] = frozenset(
    {
        "apt",
        "apt-get",
        "awk",
        "bash",
        "brew",
        "cargo",
        "chmod",
        "chown",
        "cmake",
        "cp",
        "dd",
        "deno",
        "dnf",
        "docker",
        "go",
        "gzip",
        "gunzip",
        "install",
        "kubectl",
        "ln",
        "make",
        "mkdir",
        "mv",
        "node",
        "npm",
        "pacman",
        "perl",
        "pip",
        "pip3",
        "pnpm",
        "poetry",
        "python",
        "python3",
        "rm",
        "rmdir",
        "rsync",
        "ruby",
        "sed",
        "sh",
        "systemctl",
        "tar",
        "tee",
        "touch",
        "truncate",
        "unzip",
        "uv",
        "yarn",
        "yum",
        "zsh",
        "zip",
    }
)

# ``--git-dir`` and ``--work-tree`` are deliberately absent: they point git at
# an arbitrary directory, and their ``=value`` form would slip past the path
# scan.  ``-C`` only changes the directory for the rest of the invocation.
_GIT_VALUE_GLOBAL_FLAGS: frozenset[str] = frozenset({"-C", "--namespace", "--super-prefix"})
_GIT_VALUE_GLOBAL_PREFIXES: tuple[str, ...] = ("--namespace=", "--super-prefix=")
_GIT_BOOL_GLOBAL_FLAGS: frozenset[str] = frozenset(
    {
        "-P",
        "--bare",
        "--glob-pathspecs",
        "--icase-pathspecs",
        "--literal-pathspecs",
        "--no-lazy-fetch",
        "--no-optional-locks",
        "--no-pager",
        "--no-replace-objects",
        "--noglob-pathspecs",
    }
)
# Arguments that make an otherwise read-only git command execute something or
# write a file.
_GIT_DANGEROUS_ARG_PREFIXES: tuple[str, ...] = (
    "--ext-diff",
    "--open-files-in-pager",
    "--output=",
    "--textconv",
)
_GIT_DANGEROUS_ARGS: frozenset[str] = frozenset({"-O", "--output"})

_GIT_READ_ONLY_SUBCOMMANDS: frozenset[str] = frozenset(
    {
        "blame",
        "cat-file",
        "check-attr",
        "check-ignore",
        "count-objects",
        "describe",
        "diff",
        "diff-index",
        "diff-tree",
        "for-each-ref",
        "grep",
        "log",
        "ls-files",
        "ls-tree",
        "merge-base",
        "name-rev",
        "rev-list",
        "rev-parse",
        "shortlog",
        "show",
        "show-ref",
        "status",
        "version",
        "whatchanged",
    }
)

_GIT_BRANCH_DANGEROUS_FLAGS: frozenset[str] = frozenset(
    {
        "-C",
        "-D",
        "-M",
        "-c",
        "-d",
        "-f",
        "-m",
        "-u",
        "--copy",
        "--create-reflog",
        "--delete",
        "--edit-description",
        "--force",
        "--move",
        "--no-track",
        "--set-upstream-to",
        "--track",
        "--unset-upstream",
    }
)

_GIT_TAG_DANGEROUS_FLAGS: frozenset[str] = frozenset(
    {
        "-F",
        "-a",
        "-d",
        "-e",
        "-m",
        "-s",
        "-u",
        "--annotate",
        "--delete",
        "--edit",
        "--file",
        "--local-user",
        "--message",
        "--sign",
    }
)
_GIT_TAG_LIST_PREFIXES: tuple[str, ...] = (
    "-l",
    "-n",
    "--column",
    "--contains",
    "--format",
    "--list",
    "--merged",
    "--no-contains",
    "--no-merged",
    "--omit-empty",
    "--points-at",
    "--sort",
)

_GIT_CONFIG_READ_FLAGS: frozenset[str] = frozenset(
    {
        "-l",
        "--get",
        "--get-all",
        "--get-regexp",
        "--get-urlmatch",
        "--list",
        "--show-origin",
        "--show-scope",
    }
)
_GIT_CONFIG_WRITE_FLAGS: frozenset[str] = frozenset(
    {
        "-e",
        "--add",
        "--edit",
        "--remove-section",
        "--rename-section",
        "--replace-all",
        "--unset",
        "--unset-all",
    }
)


class Verdict(StrEnum):
    """Three-valued answer to "will this command modify the workspace?".

    The bool this replaces conflated *proven* mutation with *unprovable*
    safety, which made plan mode reject read-only pipelines it could not
    static-scan (``$(...)``, ``xargs``, ``find -exec``) with no way to ask.
    """

    READ_ONLY = "read_only"
    MUTATING = "mutating"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ReadOnlyVerdict:
    """A classified command, with enough context to explain the answer."""

    verdict: Verdict
    reason: str = ""
    trigger: str = ""

    @property
    def is_read_only(self) -> bool:
        return self.verdict is Verdict.READ_ONLY


def classify_command(command: str, *, cwd: str | Path | None = None) -> ReadOnlyVerdict:
    """Classify *command* as read-only, mutating, or statically undecidable.

    ``cwd`` is the directory the command will run in; it is used to resolve
    relative paths before checking them against credential locations.
    """
    segments, split_reason = _split_segments_with_reason(command)
    if segments is None:
        return ReadOnlyVerdict(Verdict.UNKNOWN, split_reason or "unparseable", command.strip()[:80])
    try:
        resolved_cwd = (
            Path(os.path.expanduser(str(cwd))).resolve()
            if cwd is not None
            else Path.cwd().resolve()
        )
    except OSError:
        return ReadOnlyVerdict(Verdict.UNKNOWN, "cwd_unresolvable", str(cwd))

    if _is_sensitive_path(str(resolved_cwd)):
        return ReadOnlyVerdict(Verdict.MUTATING, "sensitive_path", str(resolved_cwd))

    for segment in segments:
        try:
            tokens = shlex.split(segment, posix=True)
        except ValueError:
            return ReadOnlyVerdict(Verdict.UNKNOWN, "unbalanced_quote", segment.strip()[:80])
        if not tokens:
            continue
        if not _segment_is_read_only(tokens):
            verdict, reason, trigger = _segment_failure(tokens)
            return ReadOnlyVerdict(verdict, reason, trigger)
        recursive = _segment_recurses(tokens)
        for token in _path_tokens(tokens):
            if _path_token_is_sensitive(token, resolved_cwd, recursive=recursive):
                return ReadOnlyVerdict(Verdict.MUTATING, "sensitive_path", token)
    return ReadOnlyVerdict(Verdict.READ_ONLY)


def _segment_failure(tokens: list[str]) -> tuple[Verdict, str, str]:
    """Classify a segment that :func:`_segment_is_read_only` rejected.

    Returning ``UNKNOWN`` rather than ``MUTATING`` for unrecognised commands is
    the whole point of the three-valued verdict: ``xargs`` cannot be *proven*
    read-only, but it is not evidence of a write either.  Plan mode treats the
    two differently (ADR-0013 D2).
    """
    name = _command_name(tokens[0])
    if name == "git":
        return Verdict.MUTATING, "git_subcommand", " ".join(tokens[:2])
    if name in _KNOWN_MUTATING_COMMANDS:
        return Verdict.MUTATING, "mutating_command", name
    if name not in _SAFE_COMMANDS:
        return Verdict.UNKNOWN, "unsafe_command", tokens[0]
    write_flags = _WRITE_CAPABLE_FLAGS.get(name, frozenset())
    for arg in tokens[1:]:
        for flag in _iter_flag_names(arg):
            if _flag_is_write_capable(flag, write_flags):
                return Verdict.MUTATING, "write_flag", flag
    if name == "find":
        for arg in tokens[1:]:
            if arg.startswith(_FIND_DANGEROUS_PREFIXES):
                return Verdict.MUTATING, "write_flag", arg
    if name in {"uniq", "hostname"}:
        return Verdict.MUTATING, "positional_writes", name
    return Verdict.UNKNOWN, "unclassified", name


def is_read_only_command(command: str, *, cwd: str | Path | None = None) -> bool:
    """Return True when *command* is confidently read-only.

    Thin wrapper over :func:`classify_command`; use the verdict form when the
    caller needs to distinguish "mutating" from "cannot tell".
    """
    return classify_command(command, cwd=cwd).is_read_only


def _strip_safe_redirects_with_reason(command: str) -> tuple[str | None, str | None]:
    """Remove redirections that cannot mutate the workspace.

    ``2>&1`` and ``>/dev/null`` are ubiquitous in read-only inspection
    commands; rejecting them made plan mode and default-mode auto-allow far
    stricter than the actual risk. Redirections to real files, heredocs and
    herestrings still disqualify the command.

    Returns ``(stripped, None)`` on success and ``(None, reason)`` otherwise, so
    the caller can explain *why* a command was rejected.
    """
    out: list[str] = []
    index = 0
    quote: str | None = None
    escaped = False
    while index < len(command):
        char = command[index]
        if escaped:
            out.append(char)
            escaped = False
            index += 1
            continue
        if char == "\\" and quote != "'":
            out.append(char)
            escaped = True
            index += 1
            continue
        if quote is not None:
            out.append(char)
            if char == quote:
                quote = None
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            out.append(char)
            index += 1
            continue
        if char == "&" and command[index + 1 : index + 2] == ">":
            index += 1
            if command[index : index + 1] == ">":
                index += 1
            index, safe = _consume_redirect_target(command, index, out)
            if not safe:
                return None, "redirection"
            continue
        if char in {"<", ">"}:
            _pop_fd_prefix(out)
            if char == "<" and command[index + 1 : index + 2] == "<":
                return None, "heredoc"
            if char == ">" and command[index + 1 : index + 2] == ">":
                index += 1
            index += 1
            index, safe = _consume_redirect_target(command, index, out)
            if not safe:
                return None, "redirection"
            continue
        out.append(char)
        index += 1
    if quote is not None or escaped:
        return None, "unbalanced_quote"
    return "".join(out), None


def _strip_safe_redirects(command: str) -> str | None:
    """Backwards-compatible wrapper around :func:`_strip_safe_redirects_with_reason`."""
    return _strip_safe_redirects_with_reason(command)[0]


def _pop_fd_prefix(out: list[str]) -> None:
    """Drop a leading fd number (``2`` in ``2>&1``), not a word suffix."""
    start = len(out)
    while start > 0 and out[start - 1].isdigit():
        start -= 1
    if start == len(out):
        return
    if start == 0 or out[start - 1].isspace() or out[start - 1] in ";|&(":
        del out[start:]


def _consume_redirect_target(
    command: str,
    index: int,
    out: list[str],
) -> tuple[int, bool]:
    """Consume a redirection target, returning ``(new_index, safe)``."""
    while index < len(command) and command[index] in " \t":
        index += 1
    if index >= len(command):
        return index, False
    if command[index] == "&":
        digit_start = index + 1
        index = digit_start
        while index < len(command) and command[index].isdigit():
            index += 1
        if index == digit_start:
            return index, False
        out.append(" ")
        return index, True
    start = index
    while (
        index < len(command)
        and not command[index].isspace()
        and command[index] not in ";|&<>"
    ):
        index += 1
    target = command[start:index]
    if target == "/dev/null" or (target.startswith("/dev/fd/") and target[8:].isdigit()):
        out.append(" ")
        return index, True
    return index, False


def _split_segments_with_reason(command: str) -> tuple[list[str] | None, str | None]:
    """Split a shell command, or explain why that is impossible.

    Returns ``(segments, None)`` on success and ``(None, reason)`` when the
    command uses a construct whose safety cannot be established from a static
    scan: file redirection, background execution, command or variable
    substitution, or unbalanced quoting.  The reason is surfaced to the user so
    a rejected command can be rewritten instead of guessed at.
    """
    redirect_stripped, redirect_reason = _strip_safe_redirects_with_reason(command)
    if redirect_stripped is None:
        return None, redirect_reason
    command = redirect_stripped
    segments: list[str] = []
    start = 0
    quote: str | None = None
    escaped = False
    index = 0
    while index < len(command):
        char = command[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\" and quote != "'":
            escaped = True
            index += 1
            continue
        if quote is not None:
            if char == quote:
                quote = None
            elif quote == '"' and char in {"$", "`"}:
                return None, "command_substitution"
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            index += 1
            continue
        if char in {"$", "`"}:
            return None, "command_substitution"
        if char in {"<", ">"}:
            return None, "redirection"
        if char == "&":
            if command[index + 1 : index + 2] == "&":
                segments.append(command[start:index])
                index += 2
                start = index
                continue
            return None, "background_job"
        if char == "|":
            segments.append(command[start:index])
            if command[index + 1 : index + 2] in {"|", "&"}:
                index += 2
            else:
                index += 1
            start = index
            continue
        if char in {";", "\n"}:
            segments.append(command[start:index])
            index += 1
            start = index
            continue
        index += 1
    if quote is not None or escaped:
        return None, "unbalanced_quote"
    segments.append(command[start:])
    return [segment.strip() for segment in segments if segment.strip()], None


def _split_segments(command: str) -> list[str] | None:
    """Backwards-compatible wrapper around :func:`_split_segments_with_reason`."""
    return _split_segments_with_reason(command)[0]


# Commands whose argument tokens are likely to name the file being read.  The
# bash tool exposes no ``file_path`` input, so the permission checker has to
# recover candidate paths from the command string itself.
_READ_PATH_COMMANDS: frozenset[str] = frozenset({"cat", "head", "tail", "grep", "less"})

# Commands whose *first* positional argument is a pattern rather than a path.
# Without this, ``grep -n "environ\|shadow" src`` offered the pattern itself as a
# path candidate and a backslash-escaped ``\.ssh`` was matched as ``/.ssh``.
_FIRST_ARG_IS_PATTERN: frozenset[str] = frozenset(
    {"grep", "egrep", "fgrep", "rg", "ag", "ack", "sed", "awk"}
)

_PATH_MARKERS: tuple[str, ...] = ("/", "\\", "~", "$", "`")


def paths_in_command(command: str) -> tuple[str, ...]:
    """Return tokens from *command* that may name a filesystem path.

    Bash tool invocations carry no ``file_path`` field, so credential-path
    protection has to inspect the command text.  This deliberately over-keeps
    candidates: a token is retained when it looks path-like (contains ``/``,
    ``\\``, ``~``, ``$`` or a backtick) or when it is a non-flag argument to a
    command that reads files (``cat``, ``head``, ``tail``, ``grep``, ``less``).

    ``_split_segments`` rejects substitution constructs outright; when that
    happens the raw command is tokenized instead so ``cat $HOME/.ssh/id_rsa``
    is still visible to the sensitive-path check.  The caller decides whether a
    candidate is actually sensitive; this function never resolves or opens it.
    """
    segments = _split_segments(command)
    token_groups: list[list[str]] = []
    if segments is None:
        try:
            token_groups.append(shlex.split(command, posix=True))
        except ValueError:
            return ()
    else:
        for segment in segments:
            try:
                token_groups.append(shlex.split(segment, posix=True))
            except ValueError:
                continue

    candidates: list[str] = []
    for tokens in token_groups:
        if not tokens:
            continue
        for token in _path_tokens(tokens):
            if token not in candidates:
                candidates.append(token)
    return tuple(candidates)


def _path_tokens(tokens: list[str]) -> list[str]:
    """Tokens of one segment that may name a filesystem path.

    Shared by :func:`paths_in_command` (which feeds the credential-path check)
    and :func:`classify_command` (which feeds the plan-mode decision), so the two
    cannot drift apart on what counts as a path.
    """
    if not tokens:
        return []
    command_name = _command_name(tokens[0])
    out: list[str] = []
    seen_positional = False
    for token in tokens[1:]:
        is_positional = not token.startswith("-")
        is_first_positional = is_positional and not seen_positional
        if is_positional:
            seen_positional = True
        if command_name in _FIRST_ARG_IS_PATTERN and is_first_positional:
            # ``grep PATTERN file`` — PATTERN is not a path, even when it
            # contains slashes or backslash escapes.
            continue
        looks_like_path = any(marker in token for marker in _PATH_MARKERS)
        reads_path = command_name in _READ_PATH_COMMANDS and is_positional
        if looks_like_path or reads_path:
            out.append(token)
    return out


# Option names that make an otherwise read-only command write a file or run
# another program.  Matching is done per option *name* (see
# :func:`_iter_flag_names`), never on the raw argument text: ``sort -ro f``
# carries the ``-o`` flag even though the token does not start with ``-o``.
_WRITE_CAPABLE_FLAGS: dict[str, frozenset[str]] = {
    "sort": frozenset({"-o", "--output", "--compress-program"}),
    "tree": frozenset({"-o", "--output"}),
    "fd": frozenset({"-x", "-X", "--exec", "--exec-batch"}),
    "fdfind": frozenset({"-x", "-X", "--exec", "--exec-batch"}),
    "find": frozenset(
        {
            "-delete", "-exec", "-execdir", "-ok", "-okdir",
            "-fprint", "-fprint0", "-fprintf", "-fls", "-files0-from",
        }
    ),
    "file": frozenset({"-C", "--compile"}),
    "rg": frozenset({"--pre"}),
    "tail": frozenset({"-f", "-F", "--follow"}),
    "date": frozenset({"-s", "--set"}),
}


def _iter_flag_names(arg: str) -> Iterator[str]:
    """Yield the option names carried by a single argv token.

    ``-ro`` -> ``-r``, ``-o`` (clustered short options); ``-oFILE`` -> ``-o``
    (glued value); ``--out=x`` -> ``--out``.  Non-option tokens yield nothing.
    """
    if not arg.startswith("-") or arg == "-":
        return
    if arg.startswith("--"):
        yield arg.split("=", 1)[0]
        return
    for char in arg[1:]:
        if char == "=":
            break
        yield f"-{char}"


def _split_positionals(args: list[str]) -> list[str]:
    """Return non-option arguments, honouring the ``--`` end-of-options marker."""
    positionals: list[str] = []
    for index, arg in enumerate(args):
        if arg == "--":
            return positionals + args[index + 1 :]
        if not arg.startswith("-") or arg == "-":
            positionals.append(arg)
    return positionals


def _flag_is_write_capable(flag: str, write_flags: frozenset[str]) -> bool:
    """Match exact option names, plus GNU-style unambiguous long-option prefixes.

    ``sort --out=x`` is ``--output`` to getopt_long.  Prefix matching is
    conservative: it can only widen the set of commands that need approval.
    """
    if flag in write_flags:
        return True
    return flag.startswith("--") and any(w.startswith(flag) for w in write_flags)


def _segment_is_read_only(tokens: list[str]) -> bool:
    name = _command_name(tokens[0])
    args = tokens[1:]
    if name == "git":
        return _git_is_read_only(args)
    if name not in _SAFE_COMMANDS:
        return False
    write_flags = _WRITE_CAPABLE_FLAGS.get(name, frozenset())
    if any(
        _flag_is_write_capable(flag, write_flags)
        for arg in args
        for flag in _iter_flag_names(arg)
    ):
        return False
    if name == "find":
        return not any(arg.startswith(_FIND_DANGEROUS_PREFIXES) for arg in args)
    if name == "uniq":
        # ``uniq IN OUT`` writes OUT; ``-`` counts as a positional input.
        return len(_split_positionals(args)) <= 1
    if name == "hostname":
        return not _split_positionals(args)
    return True


def _command_name(token: str) -> str:
    name = token.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name.removesuffix(".exe")


def _git_is_read_only(args: list[str]) -> bool:
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in {"--version", "--help", "-h"}:
            return True
        if arg in _GIT_VALUE_GLOBAL_FLAGS:
            index += 2
            continue
        if arg.startswith(_GIT_VALUE_GLOBAL_PREFIXES):
            index += 1
            continue
        if arg in _GIT_BOOL_GLOBAL_FLAGS:
            index += 1
            continue
        if arg.startswith("-"):
            return False
        break

    if index >= len(args):
        # A bare ``git`` prints usage.
        return True

    subcommand = args[index].lower()
    rest = args[index + 1 :]
    if any(arg in _GIT_DANGEROUS_ARGS or arg.startswith(_GIT_DANGEROUS_ARG_PREFIXES) for arg in rest):
        return False

    if subcommand in _GIT_READ_ONLY_SUBCOMMANDS:
        return True
    if subcommand == "reflog":
        return not rest or rest[0] in {"exists", "list", "show"}
    if subcommand == "branch":
        return _git_branch_is_read_only(rest)
    if subcommand == "tag":
        return _git_tag_is_read_only(rest)
    if subcommand == "remote":
        return not rest or rest[0] in {"-v", "--verbose", "get-url", "show"}
    if subcommand == "config":
        return _git_config_is_read_only(rest)
    if subcommand == "stash":
        return bool(rest) and rest[0] in {"list", "show"}
    if subcommand == "worktree":
        return rest[:1] == ["list"]
    if subcommand == "submodule":
        return rest[:1] in (["status"], ["summary"])
    return False


def _git_branch_is_read_only(args: list[str]) -> bool:
    if any(arg in _GIT_BRANCH_DANGEROUS_FLAGS for arg in args):
        return False
    # ``git branch <name>`` creates a branch, so only flag-only invocations
    # (listing filters) are safe.
    return not any(not arg.startswith("-") for arg in args)


def _git_tag_is_read_only(args: list[str]) -> bool:
    if any(arg in _GIT_TAG_DANGEROUS_FLAGS for arg in args):
        return False
    if not args:
        return True
    return any(arg.startswith(_GIT_TAG_LIST_PREFIXES) for arg in args)


def _git_config_is_read_only(args: list[str]) -> bool:
    if any(arg in _GIT_CONFIG_WRITE_FLAGS for arg in args):
        return False
    return any(arg in _GIT_CONFIG_READ_FLAGS for arg in args)


def _segment_recurses(tokens: list[str]) -> bool:
    """Return True when the segment walks a directory tree.

    Recursive reads need an extra check: a path such as ``$HOME`` is not itself
    a credential location, but a recursive grep below it would read one.
    """
    name = _command_name(tokens[0])
    args = tokens[1:]
    if name == "find":
        return True
    if name == "rg":
        return True
    if name == "grep":
        return any(
            arg in {"-r", "-R", "--recursive", "--dereference-recursive"}
            or (not arg.startswith("--") and "r" in arg[1:] and arg[1:].isalpha() and len(arg) > 2)
            for arg in args
        )
    if name == "ls":
        return any(arg in {"-R", "--recursive"} for arg in args)
    return False


def _path_token_is_sensitive(token: str, cwd: Path, *, recursive: bool) -> bool:
    if "$" in token or "`" in token:
        # The value cannot be resolved statically; require confirmation.
        return True
    # Backslashes are escapes in a shell token, not separators.  Normalising
    # them unconditionally turned a grep pattern such as ``\.ssh`` into a path
    # ``/.ssh`` and produced a hard deny for a read-only command.  ``Path``
    # handles native separators on platforms where they really are separators.
    candidate = Path(os.path.expanduser(token))
    if not candidate.is_absolute():
        candidate = cwd / candidate
    try:
        resolved = candidate.resolve()
    except OSError:
        return True
    # ``grep -r token $HOME`` would reach ``$HOME/.ssh`` even though ``$HOME``
    # is not itself a credential location.
    return _is_sensitive_path(str(resolved)) or (
        recursive and _is_ancestor_of_sensitive_root(str(resolved))
    )


#: A native Windows absolute path (``C:\...``) or UNC path (``\\server\share``).
#: Only these carry backslashes that mean "separator" rather than "escape".
_WINDOWS_ABSOLUTE_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")


def _normalize_separators(path: str) -> str:
    """Normalise separators without reinterpreting shell escapes as separators."""
    if _WINDOWS_ABSOLUTE_RE.match(path):
        return path.replace("\\", "/")
    return path


def _is_sensitive_path(path: str) -> bool:
    normalized = _normalize_separators(path).rstrip("/") or "/"
    if _contains_glob_meta(normalized):
        return _glob_reaches_sensitive_path(normalized)
    for pattern in SENSITIVE_PATH_PATTERNS:
        if fnmatch.fnmatch(normalized, pattern) or fnmatch.fnmatch(normalized + "/", pattern):
            return True
    for root in _resolved_sensitive_roots():
        if normalized == root or normalized.startswith(root + "/"):
            return True
    return False


_GLOB_META_CHARS: frozenset[str] = frozenset("*?[]{}")
# A pattern that expands to more entries than this is treated as unresolvable:
# the classifier cannot prove the command stays away from credential paths, so
# it requires confirmation instead of walking the whole filesystem.
_GLOB_MATCH_LIMIT = 2000


def _contains_glob_meta(path: str) -> bool:
    return any(char in _GLOB_META_CHARS for char in path)


def _glob_reaches_sensitive_path(pattern: str) -> bool:
    """Expand *pattern* and decide whether any match is a credential path.

    Shell expansion happens *after* the classifier sees the command, so a
    pattern such as ``.[s]sh/id_rsa`` never matches the sensitive-path patterns
    as literal text.  Expanding it here closes that gap while keeping ordinary
    globs (``ls *.py``) classified as read-only.
    """
    if "{" in pattern or "}" in pattern:
        # Brace expansion is not glob; braceexpand is a third-party behaviour
        # we cannot model, so treat it as unresolvable.
        return True
    try:
        matches = list(
            itertools.islice(
                glob_module.iglob(pattern, recursive=True),
                _GLOB_MATCH_LIMIT + 1,
            )
        )
    except (OSError, ValueError):
        return True
    if len(matches) > _GLOB_MATCH_LIMIT:
        return True
    return any(_is_sensitive_path(match) for match in matches)


def _is_ancestor_of_sensitive_root(path: str) -> bool:
    normalized = path.replace("\\", "/").rstrip("/") or "/"
    if normalized == "/":
        return True
    return any(root.startswith(normalized + "/") for root in _resolved_sensitive_roots())


def _resolved_sensitive_roots() -> tuple[str, ...]:
    roots: list[str] = []
    for root in _SENSITIVE_ROOTS:
        try:
            resolved = str(Path(os.path.expanduser(root)).resolve()).replace("\\", "/").rstrip("/")
        except OSError:
            continue
        if resolved:
            roots.append(resolved)
    return tuple(roots)
