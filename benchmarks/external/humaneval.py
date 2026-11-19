"""Official HumanEval adapter.

HumanEval is not a repository-level agent benchmark, but it is externally
maintained, bundled with its own dataset, and its grader runs without Docker or
network access.  That makes it the one official benchmark this environment can
run end to end today.

The agent receives the official prompt, uses normal tools (read/write/bash) to
produce ``solution.py``, and the official ``check_correctness`` sandbox grades
that file.  We pass an empty prompt to the grader because the agent writes the
complete function rather than a raw completion continuation.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from benchmarks.spec import BenchRun, Task

_GRADER_SCRIPT = Path(__file__).with_name("humaneval_grader.py")


def _wsl_path(path: Path) -> str:
    """Translate ``C:\\dir\\file`` into ``/mnt/c/dir/file`` for WSL."""
    resolved = path.resolve()
    drive = (resolved.drive or "C:").rstrip(":").lower()
    remainder = str(resolved)[len(resolved.drive) :].replace("\\", "/")
    return f"/mnt/{drive}{remainder}"


def _grade_official(problem: dict[str, Any], source: str, *, timeout: float = 10.0) -> dict[str, Any]:
    if sys.platform != "win32":
        from human_eval.evaluation import check_correctness

        result = check_correctness(problem, source, timeout=timeout)
        return {"passed": bool(result.get("passed")), "result": str(result.get("result"))}

    payload = {"problem": problem, "completion": source, "timeout": timeout}
    with tempfile.NamedTemporaryFile(
        "w",
        suffix=".json",
        delete=False,
        encoding="utf-8",
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False)
        payload_path = Path(handle.name)
    try:
        completed = subprocess.run(
            ["wsl.exe", "-e", "python3", _wsl_path(_GRADER_SCRIPT), _wsl_path(payload_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout + 60,
            check=False,
        )
    finally:
        payload_path.unlink(missing_ok=True)

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()[:1500]
        return {
            "passed": False,
            "result": f"WSL HumanEval grader failed (is human-eval installed in WSL?): {detail}",
        }
    try:
        return json.loads(completed.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"passed": False, "result": f"unreadable grader output: {completed.stdout[:500]}"}


def _agent_prompt(problem_prompt: str) -> str:
    return (
        "Solve this Python programming problem.\n"
        "Write a complete, runnable solution to `solution.py` in the current directory. "
        "Keep the given function signature and docstring, and do not add a test block "
        "or a __main__ block.\n\n"
        "Problem:\n"
        "```python\n"
        f"{problem_prompt.rstrip()}\n"
        "```\n"
    )


def _make_grader(problem: dict[str, str]):
    def grade(run: BenchRun) -> list[str]:
        solution = run.workspace / "solution.py"
        if not solution.exists():
            return ["solution.py was not created"]
        source = solution.read_text(encoding="utf-8", errors="replace")
        if not source.strip():
            return ["solution.py is empty"]

        # The official grader concatenates problem["prompt"] + completion. The
        # agent wrote the whole file, so grade it as a standalone program.
        result = _grade_official({**problem, "prompt": ""}, source)
        if result.get("passed"):
            return []
        return [f"HumanEval tests failed: {str(result.get('result'))[:1500]}"]
    return grade


def available_task_ids() -> list[str]:
    """Return every task id bundled with the official HumanEval package."""
    from human_eval.data import read_problems

    return list(read_problems())


def build_tasks(
    *,
    limit: int = 1,
    task_ids: list[str] | None = None,
) -> tuple[Task, ...]:
    """Return agent tasks backed by the official HumanEval dataset."""
    from human_eval.data import read_problems

    problems = read_problems()
    selected_ids = task_ids or list(problems)
    tasks: list[Task] = []
    for task_id in selected_ids:
        problem = problems.get(task_id)
        if problem is None:
            continue
        tasks.append(
            Task(
                id=task_id,
                description="Official HumanEval problem solved through the agent loop",
                prompt=_agent_prompt(problem["prompt"]),
                permission_mode="full_auto",
                max_turns=15,
                timeout_seconds=240.0,
                grade=_make_grader(dict(problem)),
                tags=("external", "humaneval", "coding"),
            )
        )
        if len(tasks) >= limit:
            break
    return tuple(tasks)


__all__ = ["available_task_ids", "build_tasks"]
