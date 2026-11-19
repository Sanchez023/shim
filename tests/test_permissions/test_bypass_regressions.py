"""Regression tests for permission-classifier bypasses.

Each case here was a working escape at some point: the classifier validated raw
argument text with ``str.startswith`` and matched credential paths before the
shell expanded globs.  Keep these green.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from openharness.permissions.command_safety import (
    _iter_flag_names,
    _split_positionals,
    is_read_only_command,
)


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    """A project directory that also holds a credential-looking file."""
    (tmp_path / "main.py").write_text("print('hi')\n", encoding="utf-8")
    secret_dir = tmp_path / ".ssh"
    secret_dir.mkdir()
    (secret_dir / "id_rsa").write_text("PRIVATE KEY\n", encoding="utf-8")
    return tmp_path


# --- flag parsing -----------------------------------------------------------


def test_clustered_short_options_are_split() -> None:
    assert set(_iter_flag_names("-ro")) == {"-r", "-o"}
    assert set(_iter_flag_names("-oFILE")) == {"-o", "-F", "-I", "-L", "-E"}


def test_long_option_value_is_stripped() -> None:
    assert set(_iter_flag_names("--output=x")) == {"--output"}


def test_positionals_honour_double_dash() -> None:
    assert _split_positionals(["-a", "--", "-o"]) == ["-o"]
    assert _split_positionals(["-", "out.txt"]) == ["-", "out.txt"]


# --- write-capable flags in a "read-only" command ---------------------------


@pytest.mark.parametrize(
    "command",
    [
        "sort -ro out.txt",
        "sort --out=x",
        "sort --output=x",
        "tree -ao /tmp/out.txt",
        "tree --output=/tmp/out.txt",
        "fd -HIx 'touch pwn'",
        "fd --exec-batch touch {}",
        "uniq - out.txt",
        "uniq in.txt out.txt",
        "find . -delete",
        "find . -fprint out.txt",
        "rg --pre 'sh -c echo' foo",
        "tail -F out.txt",
        "date -s '2020-01-01'",
    ],
)
def test_write_capable_commands_are_not_read_only(command: str) -> None:
    assert is_read_only_command(command, cwd=".") is False


# --- glob expansion before credential matching ------------------------------


def test_glob_cannot_smuggle_a_credential_path(workspace: Path) -> None:
    assert is_read_only_command("cat .[s]sh/id_rsa", cwd=workspace) is False


def test_star_glob_over_credential_dir_is_rejected(workspace: Path) -> None:
    assert is_read_only_command("cat .ss*/*", cwd=workspace) is False


def test_brace_expansion_is_treated_as_unresolvable(workspace: Path) -> None:
    assert is_read_only_command("cat {main.py,.ssh/id_rsa}", cwd=workspace) is False


def test_ordinary_globs_stay_read_only(workspace: Path) -> None:
    assert is_read_only_command("ls *.py", cwd=workspace) is True
    assert is_read_only_command("ls -la", cwd=workspace) is True
    assert is_read_only_command("grep -n print main.py", cwd=workspace) is True
    assert is_read_only_command("sort main.py | uniq -c", cwd=workspace) is True
