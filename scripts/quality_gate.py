#!/usr/bin/env python3
"""Ratchet the repository's ruff and mypy debt.

The project is migrating a large legacy codebase toward strict typing and
lint cleanliness.  This gate prevents new debt while allowing the existing
baseline to be paid down incrementally.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

BASELINE_PATH = Path(__file__).with_name("quality_baseline.json")
ROOT = Path(__file__).resolve().parents[1]
LARGE_FILE_THRESHOLD = 50_000

CORE_MYPY_PATHS = [
    "src/openharness/api",
    "src/openharness/engine",
    "src/openharness/runtime",
    "src/openharness/prompts",
    "src/openharness/services/event_log.py",
    "src/openharness/services/event_pipeline.py",
    "src/openharness/services/agent_backend.py",
    "src/openharness/services/subagent_provider.py",
    "src/openharness/services/backends",
]


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def count_ruff_errors() -> int:
    result = _run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "src",
            "tests",
            "scripts",
            "--output-format",
            "json",
        ]
    )
    if result.returncode not in {0, 1}:
        raise RuntimeError(result.stderr.strip() or "ruff failed to run")
    try:
        diagnostics = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"could not parse ruff output: {exc}") from exc
    return len(diagnostics) if isinstance(diagnostics, list) else 0


def _count_mypy_paths(paths: list[str]) -> int:
    result = _run([sys.executable, "-m", "mypy", *paths])
    if result.returncode not in {0, 1}:
        raise RuntimeError(result.stderr.strip() or "mypy failed to run")
    return sum(1 for line in result.stdout.splitlines() if ": error:" in line)


def count_mypy_errors() -> int:
    return _count_mypy_paths(["src/openharness"])


def count_core_mypy_errors() -> int:
    return _count_mypy_paths(CORE_MYPY_PATHS)


def collect_large_files() -> dict[str, int]:
    files: dict[str, int] = {}
    for path in (ROOT / "src" / "openharness").rglob("*.py"):
        size = path.stat().st_size
        if size >= LARGE_FILE_THRESHOLD:
            files[path.relative_to(ROOT).as_posix()] = size
    return files


def file_size_regressions(limits: dict[str, int]) -> list[tuple[str, int, int]]:
    regressions: list[tuple[str, int, int]] = []
    current = collect_large_files()
    for path, size in current.items():
        limit = limits.get(path)
        if limit is None or size > limit:
            regressions.append((path, size, limit or 0))
    return regressions


def stale_file_size_limits(limits: dict[str, int]) -> list[tuple[str, int, int]]:
    """Return allowances that no longer reflect the file they guard.

    The ratchet only compares growth, so an allowance recorded for a 97 KB file
    keeps permitting 97 KB after that file has been split down to 2 KB. Without
    this check a refactored file can silently regrow to its pre-split size.
    Small shrinkages are ignored: a file that shrank by a few lines is still
    guarded correctly, and failing CI for that would be noise.
    """
    current = collect_large_files()
    stale: list[tuple[str, int, int]] = []
    for path, limit in limits.items():
        actual = current.get(path)
        if actual is None:
            stale.append((path, 0, limit))
        elif limit > actual and limit > max(int(actual * 1.5), actual + 8192):
            stale.append((path, actual, limit))
    return stale


def load_baseline() -> dict[str, object]:
    if not BASELINE_PATH.exists():
        return {
            "ruff_errors": 0,
            "mypy_errors": 0,
            "mypy_core_errors": 0,
            "file_size_limits": {},
        }
    data = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    limits = data.get("file_size_limits")
    return {
        "ruff_errors": int(data.get("ruff_errors", 0)),
        "mypy_errors": int(data.get("mypy_errors", 0)),
        "mypy_core_errors": int(data.get("mypy_core_errors", 0)),
        "file_size_limits": limits if isinstance(limits, dict) else {},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--update",
        action="store_true",
        help="Record the current counts as the new baseline.",
    )
    args = parser.parse_args()

    current = {
        "ruff_errors": count_ruff_errors(),
        "mypy_errors": count_mypy_errors(),
        "mypy_core_errors": count_core_mypy_errors(),
        "file_size_limits": collect_large_files(),
    }
    if args.update:
        BASELINE_PATH.write_text(
            json.dumps(current, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"Updated {BASELINE_PATH}: {current}")
        return 0

    baseline = load_baseline()
    failed = False
    for name in ("ruff_errors", "mypy_errors", "mypy_core_errors"):
        current_count = int(current[name])
        baseline_count = int(baseline[name])
        status = "ok" if current_count <= baseline_count else "REGRESSED"
        print(
            f"{name}: {current_count} (baseline {baseline_count}) [{status}]"
        )
        if current_count > baseline_count:
            failed = True

    size_regressions = file_size_regressions(
        baseline["file_size_limits"]  # type: ignore[arg-type]
    )
    if size_regressions:
        for path, size, limit in size_regressions:
            print(
                f"large file grew: {path} {size} bytes (baseline {limit})",
                file=sys.stderr,
            )
        failed = True
    else:
        print(
            f"large files: {len(current['file_size_limits'])} tracked, no growth"
        )

    stale_limits = stale_file_size_limits(
        baseline["file_size_limits"]  # type: ignore[arg-type]
    )
    if stale_limits:
        for path, actual, limit in stale_limits:
            print(
                f"stale size allowance: {path} is {actual} bytes but the baseline "
                f"still permits {limit}",
                file=sys.stderr,
            )
        print(
            "Re-run with --update to tighten these allowances.",
            file=sys.stderr,
        )
        failed = True

    if failed:
        print(
            "Quality debt increased. Fix the new diagnostics or update the "
            "baseline explicitly with --update.",
            file=sys.stderr,
        )
        return 1
    print("Quality ratchet passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
