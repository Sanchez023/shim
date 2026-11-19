"""Run the OpenHarness benchmark suites.

Examples::

    python -m benchmarks.run --suite core
    python -m benchmarks.run --suite core --json /tmp/core.json
    python -m benchmarks.run --suite real --repeat 3 --model claude-sonnet-4-6
    python -m benchmarks.run --suite core --update-baseline benchmarks/baseline-core.json

The ``core`` suite replays a scripted model against real tools and is safe for
CI (no network, no API cost).  The ``real`` suite calls the configured model
and grades the resulting workspace.  ``--repeat`` runs every task several
times so the report can show a pass@1 rate with a Wilson confidence interval.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import math
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from benchmarks import BENCHMARK_VERSION
from benchmarks.spec import BenchRun, Task, grade_run, run_task
from benchmarks.suites import CORE_TASKS, REAL_TASKS, WIRE_TASKS

SUITES: dict[str, tuple[Task, ...]] = {
    "core": CORE_TASKS,
    "wire": WIRE_TASKS,
    "real": REAL_TASKS,
}
SUITE_CHOICES = ("core", "wire", "real", "humaneval")
OFFLINE_SUITES = frozenset({"core", "wire"})


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m benchmarks.run",
        description="Run OpenHarness agent benchmarks and write a JSON report.",
    )
    parser.add_argument("--suite", choices=SUITE_CHOICES, default="core")
    parser.add_argument("--filter", default="", help="Only run tasks whose id/description contains this text")
    parser.add_argument("--list", action="store_true", help="List matching tasks and exit")
    parser.add_argument("--limit", type=int, default=0, help="Run at most N tasks (default: all)")
    parser.add_argument("--repeat", type=int, default=1, help="Run each task N times (default 1)")
    parser.add_argument("--json", metavar="PATH", help="Write the full JSON report to PATH")
    parser.add_argument("--baseline", metavar="PATH", help="Compare against a previously saved report")
    parser.add_argument(
        "--update-baseline",
        metavar="PATH",
        help="Write this run's report to PATH as the new baseline",
    )
    parser.add_argument("--model", help="Override the model for the real suite")
    parser.add_argument("--base-url", help="Override the provider base URL for the real suite")
    parser.add_argument("--api-key", help="Override the provider API key for the real suite")
    return parser


def _selected_tasks(args: argparse.Namespace) -> list[Task]:
    if args.suite == "humaneval":
        from benchmarks.external.humaneval import available_task_ids, build_tasks

        task_ids = available_task_ids()
        if args.filter:
            needle = args.filter.lower()
            task_ids = [task_id for task_id in task_ids if needle in task_id.lower()]
        limit = args.limit or 1
        return list(build_tasks(limit=limit, task_ids=task_ids))

    tasks = list(SUITES[args.suite])
    if args.filter:
        needle = args.filter.lower()
        tasks = [
            task
            for task in tasks
            if needle in task.id.lower() or needle in task.description.lower()
        ]
    if args.limit:
        tasks = tasks[: args.limit]
    return tasks


def _resolve_real_client(args: argparse.Namespace) -> tuple[Any, str]:
    from openharness.config.settings import load_settings
    from openharness.ui.runtime import _resolve_api_client_from_settings

    settings = load_settings().merge_cli_overrides(
        model=args.model,
        base_url=args.base_url,
        api_key=args.api_key,
    )
    return _resolve_api_client_from_settings(settings), settings.model


def _git_commit() -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return completed.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _wilson_interval(successes: int, total: int, *, z: float = 1.96) -> list[float]:
    """95% Wilson score interval for a binomial pass rate."""
    if total <= 0:
        return [0.0, 0.0]
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return [round(max(0.0, centre - margin), 4), round(min(1.0, centre + margin), 4)]


def _attempt_entry(run: BenchRun) -> dict[str, Any]:
    return {
        "passed": run.passed,
        "failures": run.failures,
        "duration_seconds": round(run.duration_seconds, 2),
        "turns": run.turns,
        "tool_calls": run.tool_names,
        "questions": run.questions,
        "permission_prompts": run.permission_prompts,
        "input_tokens": run.usage.input_tokens,
        "output_tokens": run.usage.output_tokens,
        "cache_read_input_tokens": run.usage.cache_read_input_tokens,
        "cache_creation_input_tokens": run.usage.cache_creation_input_tokens,
    }


def _task_entry(task: Task, runs: list[BenchRun]) -> dict[str, Any]:
    attempts = [_attempt_entry(run) for run in runs]
    passed_attempts = sum(1 for attempt in attempts if attempt["passed"])
    failures = [
        failure
        for attempt in attempts
        if not attempt["passed"]
        for failure in attempt["failures"]
    ]
    return {
        "id": task.id,
        "description": task.description,
        "tags": list(task.tags),
        "passed": passed_attempts == len(attempts),
        "pass_rate": round(passed_attempts / len(attempts), 4) if attempts else 0.0,
        "passed_attempts": passed_attempts,
        "total_attempts": len(attempts),
        "failures": failures,
        "mean_duration_seconds": round(
            sum(attempt["duration_seconds"] for attempt in attempts) / len(attempts), 2
        )
        if attempts
        else 0.0,
        "input_tokens": sum(attempt["input_tokens"] for attempt in attempts),
        "output_tokens": sum(attempt["output_tokens"] for attempt in attempts),
        "cache_read_input_tokens": sum(
            attempt["cache_read_input_tokens"] for attempt in attempts
        ),
        "cache_creation_input_tokens": sum(
            attempt["cache_creation_input_tokens"] for attempt in attempts
        ),
        "attempts": attempts,
    }


def _dataset_info(args: argparse.Namespace) -> dict[str, Any]:
    if args.suite == "humaneval":
        from importlib.metadata import PackageNotFoundError, version

        from benchmarks.external.humaneval import available_task_ids

        try:
            package_version = version("human-eval")
        except PackageNotFoundError:
            package_version = "unknown"
        return {
            "name": "HumanEval",
            "source": "openai/human-eval",
            "package_version": package_version,
            "size": len(available_task_ids()),
        }
    return {"name": "in-repo synthetic", "size": len(SUITES[args.suite])}


def _build_report(
    args: argparse.Namespace,
    tasks: list[Task],
    runs_by_task: dict[str, list[BenchRun]],
    *,
    model: str,
    duration_seconds: float,
) -> dict[str, Any]:
    entries = [_task_entry(task, runs_by_task[task.id]) for task in tasks]
    total_attempts = sum(entry["total_attempts"] for entry in entries)
    passed_attempts = sum(entry["passed_attempts"] for entry in entries)
    passed_tasks = sum(1 for entry in entries if entry["passed"])
    return {
        "benchmark_version": BENCHMARK_VERSION,
        # requires-python >= 3.10: datetime.UTC only exists on 3.11+.
        "generated_at": datetime.now(timezone.utc).isoformat(),  # noqa: UP017
        "suite": args.suite,
        "mode": "scripted" if args.suite in OFFLINE_SUITES else "real",
        "external": args.suite == "humaneval",
        "dataset": _dataset_info(args),
        "offline": args.suite in OFFLINE_SUITES,
        "model": model,
        "repeat": args.repeat,
        "duration_seconds": round(duration_seconds, 2),
        "environment": {
            "harness_commit": _git_commit(),
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "totals": {
            "tasks": len(entries),
            "passed": passed_tasks,
            "failed": len(entries) - passed_tasks,
            "attempts": total_attempts,
            "passed_attempts": passed_attempts,
            "pass_rate": round(passed_attempts / total_attempts, 4) if total_attempts else 0.0,
            "pass_rate_ci95": _wilson_interval(passed_attempts, total_attempts),
            "input_tokens": sum(entry["input_tokens"] for entry in entries),
            "output_tokens": sum(entry["output_tokens"] for entry in entries),
            "cache_read_input_tokens": sum(
                entry["cache_read_input_tokens"] for entry in entries
            ),
            "cache_creation_input_tokens": sum(
                entry["cache_creation_input_tokens"] for entry in entries
            ),
        },
        "tasks": entries,
    }


def _write_json(path: str, payload: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _load_baseline(path: str) -> dict[str, Any] | None:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"warning: could not read baseline {path}: {exc}", file=sys.stderr)
        return None


def _compare(report: dict[str, Any], baseline: dict[str, Any]) -> list[str]:
    previous = {task["id"]: task for task in baseline.get("tasks", [])}
    regressions: list[str] = []
    for task in report["tasks"]:
        old = previous.get(task["id"])
        if old is None:
            continue
        old_rate = float(old.get("pass_rate", 1.0 if old.get("passed") else 0.0))
        if old_rate > 0 and float(task.get("pass_rate", 0.0)) < old_rate:
            regressions.append(task["id"])
    return regressions


def _print_attempt(run: BenchRun, attempt: int, repeat: int) -> None:
    status = "PASS" if run.passed else "FAIL"
    tools = ",".join(sorted(set(run.tool_names))) or "-"
    suffix = f" ({attempt + 1}/{repeat})" if repeat > 1 else ""
    print(
        f"[{status}] {run.task.id:<26} {run.duration_seconds:6.2f}s  tools={tools}{suffix}",
        flush=True,
    )
    for failure in run.failures:
        indented = "\n".join(f"        {line}" for line in str(failure).splitlines())
        print(indented, flush=True)


async def _close_client(client: Any) -> None:
    close = getattr(client, "close", None)
    if close is None:
        return
    result = close()
    if inspect.isawaitable(result):
        await result


async def _run(args: argparse.Namespace) -> int:
    tasks = _selected_tasks(args)
    if args.list:
        for task in tasks:
            print(f"{task.id:<26} [{task.permission_mode}] {task.description}")
        return 0

    if not tasks:
        print("no benchmark tasks matched the filter", file=sys.stderr)
        return 2
    if args.repeat < 1:
        print("--repeat must be >= 1", file=sys.stderr)
        return 2

    scripted = args.suite in OFFLINE_SUITES
    client: Any = None
    model = "scripted"
    if not scripted:
        client, model = _resolve_real_client(args)

    if args.suite == "humaneval":
        label = "official HumanEval (real model)"
    elif args.suite == "wire":
        label = "provider wire layer (offline)"
    elif scripted:
        label = "harness conformance (scripted)"
    else:
        label = "capability smoke (real model)"
    print(
        f"OpenHarness benchmark · suite={args.suite} [{label}] "
        f"model={model} tasks={len(tasks)} repeat={args.repeat}",
        flush=True,
    )

    started = time.monotonic()
    runs_by_task: dict[str, list[BenchRun]] = {task.id: [] for task in tasks}
    try:
        for attempt in range(args.repeat):
            for task in tasks:
                if task.runner is not None:
                    run = grade_run(task, await task.runner(task))
                else:
                    run = await run_task(
                        task,
                        scripted=scripted,
                        client=client,
                        model=model,
                    )
                runs_by_task[task.id].append(run)
                _print_attempt(run, attempt, args.repeat)
    finally:
        await _close_client(client)

    duration = time.monotonic() - started
    report = _build_report(
        args,
        tasks,
        runs_by_task,
        model=model,
        duration_seconds=duration,
    )
    totals = report["totals"]
    low, high = totals["pass_rate_ci95"]
    print(
        f"\nSummary: {totals['passed']}/{totals['tasks']} tasks passed; "
        f"{totals['passed_attempts']}/{totals['attempts']} attempts passed "
        f"({totals['pass_rate'] * 100:.1f}%, 95% CI {low * 100:.1f}–{high * 100:.1f}%) "
        f"in {duration:.1f}s",
        flush=True,
    )
    if not scripted:
        print(
            "Tokens: "
            f"input={totals['input_tokens']} output={totals['output_tokens']} "
            f"cache_read={totals['cache_read_input_tokens']} "
            f"cache_write={totals['cache_creation_input_tokens']}",
            flush=True,
        )

    regressions: list[str] = []
    if args.baseline:
        baseline = _load_baseline(args.baseline)
        if baseline is not None:
            regressions = _compare(report, baseline)
            old = baseline.get("totals", {})
            print(
                "Baseline: "
                f"{old.get('passed', '?')}/{old.get('tasks', '?')} tasks, "
                f"{old.get('pass_rate', '?')} pass rate → "
                f"{totals['passed']}/{totals['tasks']} tasks, {totals['pass_rate']} pass rate",
                flush=True,
            )
            if regressions:
                print(f"Regressions: {', '.join(regressions)}", flush=True)

    if args.json:
        _write_json(args.json, report)
        print(f"Report written to {args.json}", flush=True)
    if args.update_baseline:
        _write_json(args.update_baseline, report)
        print(f"Baseline updated at {args.update_baseline}", flush=True)

    return 0 if totals["failed"] == 0 and not regressions else 1


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
