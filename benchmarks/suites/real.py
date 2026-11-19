"""Real-model capability suite.

These tasks run the configured model through the full agent loop.  Grading
looks at the resulting workspace (tests passing) and at the agent's behaviour
(whether it asked before planning, whether it answered from the repository).
They cost API tokens and are never run by default.
"""

from __future__ import annotations

from benchmarks.spec import BenchRun, Task, run_pytest

_CALCULATOR = {
    "calculator.py": "def add(a, b):\n    return a - b\n",
    "test_calculator.py": (
        "from calculator import add\n\n"
        "def test_add():\n"
        "    assert add(2, 3) == 5\n"
    ),
}

_TEXTSTATS = {
    "textstats.py": (
        '"""Text statistics helpers."""\n\n\n'
        "def word_count(text: str) -> int:\n"
        '    """Return the number of whitespace-separated words in *text*."""\n'
        "    raise NotImplementedError\n"
    ),
    "test_textstats.py": (
        "from textstats import word_count\n\n"
        "def test_word_count_basic():\n"
        '    assert word_count("one two three") == 3\n\n'
        "def test_word_count_empty():\n"
        '    assert word_count("   ") == 0\n\n'
        "def test_word_count_punctuation():\n"
        '    assert word_count("hello, world!") == 2\n'
    ),
}

_LOCATE = {
    "svc/__init__.py": "",
    "svc/retry.py": (
        "RETRY_LIMIT = 7\n\n\n"
        "def send_request(payload):\n"
        "    for _ in range(RETRY_LIMIT):\n"
        "        payload = payload\n"
        "    return payload\n"
    ),
    "app.py": (
        "from svc.retry import send_request\n\n\n"
        "def main():\n"
        "    return send_request({})\n"
    ),
}

_PLAN_FIXTURE = {
    "README.md": "# Cache Service\n\nA tiny service used by the benchmark.\n",
    "cache_service.py": (
        "USERS = {'u1': {'id': 'u1', 'name': 'Ada'}}\n\n\n"
        "def get_user(user_id):\n"
        "    # Simulates a slow database round trip.\n"
        "    return USERS.get(user_id)\n"
    ),
}


def _grade_tests_pass(run: BenchRun) -> list[str]:
    passed, output = run_pytest(run.workspace)
    if passed:
        return []
    return [f"pytest should pass after the task:\n{output}"]


def _grade_plan_clarifies(run: BenchRun) -> list[str]:
    failures: list[str] = []
    if not run.questions:
        failures.append(
            "model started planning without asking a clarifying question; "
            f"ask_user_question calls: {run.questions}"
        )
    if not run.final_text().strip():
        failures.append("model produced no plan text")
    successful_mutations = [
        result
        for result in run.results
        if result["name"] in {"write_file", "edit_file"} and result["ok"]
    ]
    if successful_mutations:
        failures.append("plan mode performed a file mutation")
    return failures


def _grade_locate(run: BenchRun) -> list[str]:
    text = run.final_text()
    failures = [
        f"answer did not mention {needle!r}"
        for needle in ("retry.py", "RETRY_LIMIT", "send_request")
        if needle not in text
    ]
    mutations = [
        result
        for result in run.results
        if result["name"] in {"write_file", "edit_file"} and result["ok"]
    ]
    if mutations:
        failures.append("a read-only question modified the workspace")
    return failures


REAL_TASKS: tuple[Task, ...] = (
    Task(
        id="fix-failing-test",
        description="Find and fix a bug so the repository's test passes",
        prompt=(
            "The test in test_calculator.py is failing. Find the bug in "
            "calculator.py, fix it, and run the test to verify."
        ),
        fixture=_CALCULATOR,
        permission_mode="full_auto",
        max_turns=30,
        timeout_seconds=300.0,
        grade=_grade_tests_pass,
        tags=("coding", "debug"),
    ),
    Task(
        id="implement-function",
        description="Implement a documented function so its tests pass",
        prompt="Implement word_count in textstats.py so the tests pass, then run the tests.",
        fixture=_TEXTSTATS,
        permission_mode="full_auto",
        max_turns=30,
        timeout_seconds=300.0,
        grade=_grade_tests_pass,
        tags=("coding", "feature"),
    ),
    Task(
        id="locate-code",
        description="Locate a constant and its callers without modifying the repo",
        prompt=(
            "Find where RETRY_LIMIT is defined and which functions reference it. "
            "Answer with file paths and function names."
        ),
        fixture=_LOCATE,
        permission_mode="default",
        max_turns=20,
        timeout_seconds=240.0,
        grade=_grade_locate,
        tags=("research", "tools"),
    ),
    Task(
        id="plan-clarifies",
        description="Plan mode asks about ambiguous requirements before planning",
        prompt=(
            "This service is getting slow. Add caching to the project and prepare "
            "an implementation plan."
        ),
        fixture=_PLAN_FIXTURE,
        permission_mode="plan",
        max_turns=20,
        timeout_seconds=240.0,
        ask_reply=(
            "Use an in-memory TTL cache with a 60-second TTL, keep the get_user "
            "signature unchanged, and do not add dependencies."
        ),
        grade=_grade_plan_clarifies,
        tags=("plan", "ask"),
    ),
)

__all__ = ["REAL_TASKS"]
