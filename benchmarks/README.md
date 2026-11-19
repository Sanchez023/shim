# OpenHarness benchmarks

Two small suites that measure different things:

| Suite | Model | Cost | What it measures |
|---|---|---|---|
| `core` | scripted transcript | none | **Harness conformance**: permissions, tool execution, plan-mode wiring, output draining |
| `real` | configured model | API tokens | **Capability smoke**: can the agent fix code, implement a function, answer a repository question, and ask before planning |

```bash
# Deterministic regression suite (safe for CI)
python -m benchmarks.run --suite core

# Real-model capability smoke test (uses the configured provider/model)
python -m benchmarks.run --suite real --repeat 3

# Save / compare a baseline
python -m benchmarks.run --suite core --update-baseline benchmarks/baseline-core.json
python -m benchmarks.run --suite core --baseline benchmarks/baseline-core.json

# List or filter tasks
python -m benchmarks.run --list
python -m benchmarks.run --suite real --filter plan
```

The JSON report records the harness commit, Python/platform, model, per-task
attempts, tool calls, questions, permission prompts, token usage, and a
pass@1 rate with a 95% Wilson confidence interval when `--repeat > 1`.

## External: HumanEval

```bash
python -m benchmarks.run --suite humaneval --limit 5
```

Uses OpenAI's official `human-eval` dataset and its official
`check_correctness` grader. The agent receives the official prompt, writes
`solution.py` with normal tools, and the official tests grade that file.

- Linux/macOS: `uv sync --extra benchmark` (or `pip install '.[benchmark]'`).
- Windows: the official grader uses `signal.setitimer`, which does not exist on
  Windows. The adapter runs the same official `check_correctness` through WSL,
  so install it there once:
  `wsl -e python3 -m pip install --user --break-system-packages human-eval`.
- One problem is an integration smoke test, not a pass@1 estimate. Use
  `--limit 20` or more for a number worth reporting.

**What this measures:** single-file Python code generation through the agent
loop, with an externally maintained dataset and grader. It is *not* a
repository-level coding benchmark.

**Why not SWE-bench / Terminal-Bench yet:** their official harnesses require
Docker images (Docker Hub / ghcr). On the machine this adapter was developed
on, Docker is absent and Docker Hub is unreachable, so those adapters cannot
run end to end. The integration point is ready: a future adapter produces a
`predictions.jsonl` and shells out to the official harness when Docker is
available.

## What this benchmark can and cannot claim

**It can claim:** a regression signal for the behaviours it names. The `core`
suite is deterministic: if it passes, read-only commands are auto-allowed,
mutating commands prompt, credential paths prompt, parallel reads work, large
output drains, edits are verified by pytest, and `ask_user_question` is wired
through plan mode. Those assertions are cheap and stable enough for CI.

**It cannot claim:** that the agent is "good" in general, or that model A is
better than model B. The sample is tiny (7 scripted + 4 real tasks), the tasks
are authored in this repository (so they can be overfit), and the real suite
is noisy at one attempt per task.

Treat the `real` suite as a smoke test, not a leaderboard. For claims that
compare agents or models publicly, use an externally maintained benchmark with
its official harness, for example:

- **SWE-bench Verified / Lite** — real GitHub issues, hidden tests, Docker.
- **Terminal-Bench** — long-horizon terminal tasks.
- **τ-bench / WebArena** — tool-use and environment interaction.

A future adapter can run those harnesses against `openharness`; this in-repo
suite deliberately does not pretend to replace them.

## Keeping results credible

1. Run `--repeat 3` or more for the real suite and report the pass rate with
   its confidence interval, not a single green run.
2. Pin the model and provider version, and record the harness commit (the
   report already does this).
3. Grade on artifacts, not prose. The coding tasks run the fixture's tests;
   avoid adding text-matching graders where a test can decide the outcome.
4. Keep tasks out of the prompt-tuning loop. If a task starts passing because
   the prompt was changed to mention that exact task, the result is overfit.
5. Rotate or add tasks instead of editing expectations to match current
   behaviour.
6. Compare like with like: same model, same permission mode, same repeat count,
   same machine. `core` results are not comparable across harness versions
   without the saved baseline.

## Adding a task

Create a `Task` in `benchmarks/suites/core.py` (scripted) or
`benchmarks/suites/real.py` (real model):

```python
Task(
    id="my-task",
    description="One sentence describing the behaviour",
    prompt="What the agent sees",
    fixture={"app.py": "..."},
    permission_mode="default",
    grade=lambda run: [] if run.passed else ["why it failed"],
)
```

Graders return a list of failure strings; an empty list means pass. Use the
helpers in `benchmarks/spec.py` (`expect_tool`, `expect_tool_ok`,
`expect_permission_prompt`, `run_pytest`) so failures are consistent.
