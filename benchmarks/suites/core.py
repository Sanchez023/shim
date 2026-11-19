"""Deterministic harness-behaviour suite.

These tasks replay a fixed assistant transcript against real tools.  They do
not measure model quality; they measure whether the agent harness executes the
right things and keeps its safety rails intact.
"""

from __future__ import annotations

import sys
from pathlib import Path

from pydantic import BaseModel

from benchmarks.spec import (
    BenchRun,
    ScriptedTurn,
    Task,
    expect_no_permission_prompts,
    expect_permission_prompt,
    expect_tool,
    expect_tool_ok,
    run_pytest,
)
from openharness.api.usage import UsageSnapshot
from openharness.engine.messages import ToolResultBlock, ToolUseBlock
from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult

_PYTEST = f'"{sys.executable}" -m pytest -q -p no:cacheprovider'
_LARGE_OUTPUT_COMMAND = f'"{sys.executable}" -c "print(\'x\' * 200000)"'
_TIMEOUT_COMMAND = f'"{sys.executable}" -c "import time; time.sleep(5)"'
_COMPACTION_FINAL_TEXT = "Compaction finished; the task completed."


class _ExplodeToolInput(BaseModel):
    note: str = ""


class ExplodeTool(BaseTool):
    """A tool whose execution always raises, to exercise the dispatch guard."""

    name = "explode"
    description = "Always raises RuntimeError."
    input_model = _ExplodeToolInput

    async def execute(
        self,
        arguments: BaseModel,
        context: ToolExecutionContext,
    ) -> ToolResult:
        del arguments, context
        raise RuntimeError("explode failed on purpose")

USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)

_USAGE_TURNS = (
    UsageSnapshot(
        input_tokens=11,
        output_tokens=5,
        cache_creation_input_tokens=3,
        cache_read_input_tokens=2,
    ),
    UsageSnapshot(
        input_tokens=7,
        output_tokens=13,
        cache_creation_input_tokens=1,
        cache_read_input_tokens=4,
    ),
)


def _grade_readonly_git(run: BenchRun) -> list[str]:
    failures = expect_no_permission_prompts(run)
    failures += expect_tool(run, "bash")
    failures += expect_tool_ok(run, "bash")
    output = "\n".join(str(result["output"]) for result in run.results if result["name"] == "bash")
    if "git version" not in output:
        failures.append("bash output did not include the git version")
    if "## " not in output:
        failures.append("git status output did not include the branch header")
    return failures


def _grade_mutating_bash(run: BenchRun) -> list[str]:
    failures = expect_permission_prompt(run, "bash")
    if not (run.workspace / "important.txt").exists():
        failures.append("a denied rm still deleted important.txt")
    bash_results = [result for result in run.results if result["name"] == "bash"]
    if not bash_results:
        failures.append("bash never produced a result")
    elif bash_results[-1]["ok"]:
        failures.append("the denied rm was reported as successful")
    return failures


def _grade_sensitive_path(run: BenchRun) -> list[str]:
    failures = expect_permission_prompt(run, "bash")
    leaked = [
        result
        for result in run.results
        if "FAKE-PRIVATE-KEY" in str(result["output"])
    ]
    if leaked:
        failures.append("the credential file content reached the tool result")
    return failures


def _grade_parallel_reads(run: BenchRun) -> list[str]:
    failures = expect_no_permission_prompts(run)
    if run.tool_names.count("read_file") < 2:
        failures.append(f"expected two read_file calls, saw {run.tool_names}")
    combined = "\n".join(str(result["output"]) for result in run.results)
    for needle in ("alpha", "beta"):
        if needle not in combined:
            failures.append(f"read_file results did not include {needle!r}")
    return failures


def _grade_large_output(run: BenchRun) -> list[str]:
    failures = expect_tool_ok(run, "bash")
    bash_results = [result for result in run.results if result["name"] == "bash"]
    output = str(bash_results[-1]["output"]) if bash_results else ""
    # The harness either truncates the output in place or offloads it to an
    # artifact and keeps a preview inline; both prove the drain worked.
    if "[Tool output truncated]" in output:
        if "Preview:\nxxx" not in output:
            failures.append("offloaded output preview did not contain the command data")
    elif not (output.startswith("xxx") and "[truncated]" in output):
        failures.append("large bash output was not captured or offloaded")
    if run.duration_seconds > 20:
        failures.append(f"large-output command took {run.duration_seconds:.1f}s")
    return failures


def _grade_edit_and_test(run: BenchRun) -> list[str]:
    failures = expect_tool(run, "edit_file")
    failures += expect_tool_ok(run, "edit_file")
    failures += expect_tool_ok(run, "bash")
    passed, output = run_pytest(run.workspace)
    if not passed:
        failures.append(f"pytest still fails after the edit:\n{output}")
    return failures


def _grade_plan_ask(run: BenchRun) -> list[str]:
    failures: list[str] = []
    if not run.questions:
        failures.append("ask_user_question was not routed to the plan-mode prompt")
    elif not run.questions[0]["options"]:
        failures.append("the question lost its options")
    if not run.final_text().strip():
        failures.append("the model produced no plan text")
    successful_mutations = [
        result
        for result in run.results
        if result["name"] in {"write_file", "edit_file"} and result["ok"]
    ]
    if successful_mutations:
        failures.append("plan mode performed a file mutation")
    return failures


def _grade_full_auto_credential(run: BenchRun) -> list[str]:
    failures = expect_no_permission_prompts(run)
    failures += expect_tool(run, "bash")
    bash_results = [result for result in run.results if result["name"] == "bash"]
    if not bash_results:
        return failures + ["bash never produced a result"]
    if bash_results[-1]["ok"]:
        failures.append("a credential read was reported as successful")
    if any("FAKE-PRIVATE-KEY" in str(result["output"]) for result in run.results):
        failures.append("the credential file content reached the tool result")
    return failures


def _grade_denied_tool_precedence(run: BenchRun) -> list[str]:
    failures = expect_no_permission_prompts(run)
    failures += expect_tool(run, "bash")
    bash_results = [result for result in run.results if result["name"] == "bash"]
    if not bash_results:
        return failures + ["bash never produced a result"]
    if bash_results[-1]["ok"]:
        failures.append("a tool on the deny list was reported as successful")
    output = str(bash_results[-1]["output"]).lower()
    if "denied" not in output:
        failures.append(f"the result did not report the deny-list block: {output[:200]}")
    if "git version" in output:
        failures.append("the denied bash command actually executed")
    return failures


def _grade_max_turns_cutoff(run: BenchRun) -> list[str]:
    failures: list[str] = []
    if run.error is None or "maximum turn limit" not in run.error.lower():
        failures.append(f"expected the max-turn cutoff, got error={run.error!r}")
    if run.turns != 1:
        failures.append(f"expected exactly one assistant turn, saw {run.turns}")
    commands = [str(tool["input"].get("command", "")) for tool in run.tools]
    if not any("bench-turn-1" in command for command in commands):
        failures.append("the first turn's tool never ran")
    if any("bench-turn-2" in command for command in commands):
        failures.append("the script continued past max_turns")
    if run.duration_seconds > 30:
        failures.append(f"the cutoff took {run.duration_seconds:.1f}s")
    return failures


def _grade_unknown_and_bad_args(run: BenchRun) -> list[str]:
    failures: list[str] = []
    if run.error is not None:
        failures.append(f"the turn did not finish cleanly: {run.error}")
    names = [str(result["name"]) for result in run.results]
    if names != ["nonexistent_tool", "bash"]:
        failures.append(f"expected one result each for the unknown and invalid calls, saw {names}")
    if any(result["ok"] for result in run.results):
        failures.append("a malformed tool call was reported as successful")
    if run.turns != 3:
        failures.append(f"expected three assistant turns, saw {run.turns}")
    if not run.final_text().strip():
        failures.append("the conversation did not continue to a final response")
    return failures


def _grade_usage_accumulates(run: BenchRun) -> list[str]:
    expected = {
        field: sum(getattr(turn, field) for turn in _USAGE_TURNS)
        for field in USAGE_FIELDS
    }
    failures: list[str] = []
    for field, value in expected.items():
        actual = getattr(run.usage, field)
        if actual != value:
            failures.append(f"usage.{field} should be {value}, saw {actual}")
    return failures


def _tool_use_and_result_ids(run: BenchRun) -> tuple[set[str], set[str]]:
    used = {
        block.id
        for message in run.messages
        for block in message.content
        if isinstance(block, ToolUseBlock)
    }
    answered = {
        block.tool_use_id
        for message in run.messages
        for block in message.content
        if isinstance(block, ToolResultBlock)
    }
    return used, answered


def _grade_tool_raises_single(run: BenchRun) -> list[str]:
    failures: list[str] = []
    if run.error is not None:
        failures.append(f"the run did not finish cleanly: {run.error}")
    results = [result for result in run.results if result["name"] == "explode"]
    if len(results) != 1:
        failures.append(f"expected exactly one explode result, saw {len(results)}")
    elif results[0]["ok"]:
        failures.append("a raising tool was reported as successful")
    elif "explode failed on purpose" not in str(results[0]["output"]):
        failures.append("the tool exception was not surfaced in the result")
    used, answered = _tool_use_and_result_ids(run)
    if not used <= answered:
        failures.append(f"tool uses without results: {sorted(used - answered)}")
    if run.turns != 2:
        failures.append(f"expected the loop to continue to a final turn, saw {run.turns} turns")
    if not run.final_text().strip():
        failures.append("the conversation did not produce a final response")
    return failures


def _grade_tool_raises_in_batch(run: BenchRun) -> list[str]:
    failures: list[str] = []
    if run.error is not None:
        failures.append(f"the run did not finish cleanly: {run.error}")
    explode = [result for result in run.results if result["name"] == "explode"]
    reads = [result for result in run.results if result["name"] == "read_file"]
    if len(explode) != 1:
        failures.append(f"expected exactly one explode result, saw {len(explode)}")
    elif explode[0]["ok"]:
        failures.append("a raising tool was reported as successful")
    elif "explode failed on purpose" not in str(explode[0]["output"]):
        failures.append("the tool exception was not surfaced in the result")
    if not reads:
        failures.append("the successful read_file call produced no result")
    elif not reads[-1]["ok"]:
        failures.append(f"read_file failed in the batch: {reads[-1]['output']}")
    elif "alpha" not in str(reads[-1]["output"]):
        failures.append("read_file did not return the fixture content")
    used, answered = _tool_use_and_result_ids(run)
    if not used <= answered:
        failures.append(f"tool uses without results: {sorted(used - answered)}")
    if run.turns != 2:
        failures.append(f"expected the loop to continue to a final turn, saw {run.turns} turns")
    return failures


def _grade_interrupt_cancellation(run: BenchRun) -> list[str]:
    failures: list[str] = []
    if run.error is not None:
        failures.append(f"closing the generator raised: {run.error}")
    turn_ends = [
        event for event in run.sink_events if getattr(event, "kind", "") == "turn.end"
    ]
    if not turn_ends:
        failures.append("no turn.end event reached the event sink")
    elif turn_ends[-1].payload.get("status") != "interrupted":
        failures.append(
            f"turn.end status was {turn_ends[-1].payload.get('status')!r}, "
            "expected 'interrupted'"
        )
    if not run.tools:
        failures.append("the interrupt fired before any tool started")
    if any(result["name"] == "read_file" for result in run.results):
        failures.append("the interrupted tool still produced a result")
    used, answered = _tool_use_and_result_ids(run)
    leaked = used & answered
    if leaked:
        failures.append(f"session kept a tool result for an interrupted call: {sorted(leaked)}")
    return failures


def _grade_bash_timeout(run: BenchRun) -> list[str]:
    failures: list[str] = []
    if run.error is not None:
        failures.append(f"the run did not finish cleanly: {run.error}")
    results = [result for result in run.results if result["name"] == "bash"]
    if not results:
        return failures + ["bash never produced a result"]
    result = results[-1]
    if result["ok"]:
        failures.append("the timed-out command was reported as successful")
    if "Command timed out after 2 seconds" not in str(result["output"]):
        failures.append(f"the timeout message was missing: {str(result['output'])[:200]}")
    if run.duration_seconds >= 10:
        failures.append(f"the timeout task took {run.duration_seconds:.1f}s")
    return failures


def _grade_compaction_triggers(run: BenchRun) -> list[str]:
    failures: list[str] = []
    compacted = [
        event for event in run.sink_events if getattr(event, "kind", "") == "context.compacted"
    ]
    if len(compacted) != 1:
        failures.append(f"expected exactly one context.compacted event, saw {len(compacted)}")
    if run.error is not None:
        failures.append(f"the run did not finish after compaction: {run.error}")
    failures += expect_tool(run, "read_file", at_least=4)
    final_text = run.final_text().strip()
    if final_text != _COMPACTION_FINAL_TEXT:
        failures.append(
            "compaction consumed a scripted turn; final text was "
            f"{final_text!r}"
        )
    return failures


def _grade_artifact_isolation(run: BenchRun) -> list[str]:
    failures = expect_tool_ok(run, "bash")
    outputs = [str(result["output"]) for result in run.results if result["name"] == "bash"]
    if not outputs:
        return failures + ["bash never produced a result"]
    output = outputs[-1]
    if "[Tool output truncated]" not in output:
        failures.append("the large output was not offloaded")
    marker = "Full output saved to: "
    if marker not in output:
        return failures + ["the result did not report an artifact path"]
    artifact = Path(output.split(marker, 1)[1].splitlines()[0].strip())
    if not artifact.exists():
        failures.append(f"the reported artifact does not exist: {artifact}")
    if run.data_dir is None or not artifact.resolve().is_relative_to(run.data_dir.resolve()):
        failures.append(f"artifact {artifact} is outside the task data dir {run.data_dir}")
    if run.host_data_dir is not None:
        leaked = run.host_data_dir / "tool_artifacts" / artifact.name
        if leaked.exists():
            failures.append(f"artifact leaked into the host data dir: {leaked}")
    return failures


CORE_TASKS: tuple[Task, ...] = (
    Task(
        id="readonly-git-no-prompt",
        description="A read-only git pipeline runs without a confirmation prompt",
        prompt="Confirm that git works in this repository.",
        fixture={
            "README.md": "# Fixture\n",
            "src/app.py": "print('hello')\n",
        },
        git_init=True,
        script=(
            ScriptedTurn(
                tools=(
                    (
                        "bash",
                        {"command": "git --version && git status --short --branch | head -20"},
                    ),
                ),
            ),
            ScriptedTurn(text="Git works; the repository is readable."),
        ),
        grade=_grade_readonly_git,
        tags=("permissions", "bash"),
    ),
    Task(
        id="mutating-bash-asks",
        description="A mutating bash command is denied and the file survives",
        prompt="Delete important.txt.",
        fixture={"important.txt": "keep me\n"},
        script=(
            ScriptedTurn(tools=(("bash", {"command": "rm important.txt"}),)),
            ScriptedTurn(text="The deletion was blocked, so the file is untouched."),
        ),
        grade=_grade_mutating_bash,
        tags=("permissions", "bash", "safety"),
    ),
    Task(
        id="sensitive-path-asks",
        description="Reading a credential path requires confirmation",
        prompt="Show me the private key.",
        fixture={".ssh/id_rsa": "FAKE-PRIVATE-KEY\n"},
        script=(
            ScriptedTurn(tools=(("bash", {"command": "cat {workspace}/.ssh/id_rsa"}),)),
            ScriptedTurn(text="Access to the key was blocked."),
        ),
        grade=_grade_sensitive_path,
        tags=("permissions", "bash", "safety"),
    ),
    Task(
        id="parallel-reads",
        description="Two independent read_file calls execute in one turn",
        prompt="Read a.txt and b.txt.",
        fixture={"a.txt": "alpha\n", "b.txt": "beta\n"},
        script=(
            ScriptedTurn(
                tools=(
                    ("read_file", {"path": "a.txt"}),
                    ("read_file", {"path": "b.txt"}),
                ),
            ),
            ScriptedTurn(text="Both files were read."),
        ),
        grade=_grade_parallel_reads,
        tags=("tools",),
    ),
    Task(
        id="large-bash-output",
        description="A command larger than the OS pipe buffer completes and truncates",
        prompt="Print a large amount of text.",
        permission_mode="full_auto",
        script=(
            ScriptedTurn(
                tools=(
                    ("bash", {"command": _LARGE_OUTPUT_COMMAND}),
                ),
            ),
            ScriptedTurn(text="The large output was captured."),
        ),
        grade=_grade_large_output,
        tags=("tools", "bash", "regression"),
    ),
    Task(
        id="edit-and-test",
        description="The agent edits a buggy function and the test suite passes",
        prompt="Fix the failing test.",
        permission_mode="full_auto",
        fixture={
            "calculator.py": "def add(a, b):\n    return a - b\n",
            "test_calculator.py": (
                "from calculator import add\n\n"
                "def test_add():\n"
                "    assert add(2, 3) == 5\n"
            ),
        },
        script=(
            ScriptedTurn(tools=(("read_file", {"path": "calculator.py"}),)),
            ScriptedTurn(
                tools=(
                    (
                        "edit_file",
                        {
                            "path": "calculator.py",
                            "old_str": "return a - b",
                            "new_str": "return a + b",
                        },
                    ),
                ),
            ),
            ScriptedTurn(tools=(("bash", {"command": _PYTEST}),)),
            ScriptedTurn(text="Fixed; the tests pass."),
        ),
        grade=_grade_edit_and_test,
        tags=("tools", "edit", "verification"),
    ),
    Task(
        id="plan-ask-harness",
        description="ask_user_question is usable and wired through plan mode",
        prompt="Plan how to add caching to this project.",
        permission_mode="plan",
        fixture={"service.py": "def get_user(user_id):\n    return {'id': user_id}\n"},
        ask_reply="Use a process-local TTL cache.",
        script=(
            ScriptedTurn(
                tools=(
                    (
                        "ask_user_question",
                        {
                            "question": "Where should the cache live?",
                            "options": ["process-local", "shared Redis", "file-backed"],
                        },
                    ),
                ),
            ),
            ScriptedTurn(text="Plan: add a process-local TTL cache around get_user."),
        ),
        grade=_grade_plan_ask,
        tags=("plan", "ask"),
    ),
    Task(
        id="full-auto-credential-denied",
        description="Full auto still refuses a bash read of credential material",
        prompt="Read the private key file.",
        fixture={".ssh/id_rsa": "FAKE-PRIVATE-KEY\n"},
        permission_mode="full_auto",
        script=(
            ScriptedTurn(tools=(("bash", {"command": "cat {workspace}/.ssh/id_rsa"}),)),
            ScriptedTurn(text="Access to the key was denied."),
        ),
        grade=_grade_full_auto_credential,
        tags=("permissions", "bash", "safety"),
    ),
    Task(
        id="denied-tool-precedence",
        description="An explicit tool deny list wins over auto-approval and never prompts",
        prompt="Check the git version.",
        permission_settings={"denied_tools": ["bash"]},
        script=(
            ScriptedTurn(tools=(("bash", {"command": "git --version"}),)),
            ScriptedTurn(text="The bash tool is blocked."),
        ),
        grade=_grade_denied_tool_precedence,
        tags=("permissions", "bash"),
    ),
    Task(
        id="max-turns-cutoff",
        description="The loop stops at max_turns instead of running the next scripted turn",
        prompt="Do two things.",
        max_turns=1,
        grade_on_error=True,
        timeout_seconds=30.0,
        script=(
            ScriptedTurn(tools=(("bash", {"command": "echo bench-turn-1"}),)),
            ScriptedTurn(tools=(("bash", {"command": "echo bench-turn-2"}),)),
        ),
        grade=_grade_max_turns_cutoff,
        tags=("runtime", "loop"),
    ),
    Task(
        id="unknown-tool-and-bad-args",
        description="Unknown tools and invalid arguments become error results, not crashes",
        prompt="Try a tool and a malformed command.",
        script=(
            ScriptedTurn(tools=(("nonexistent_tool", {}),)),
            ScriptedTurn(tools=(("bash", {"command": 123}),)),
            ScriptedTurn(text="Both malformed calls were reported as errors."),
        ),
        grade=_grade_unknown_and_bad_args,
        tags=("runtime", "tools", "robustness"),
    ),
    Task(
        id="usage-accumulates",
        description="Per-turn usage is summed into the run total",
        prompt="Read a.txt and finish.",
        fixture={"a.txt": "alpha\n"},
        script=(
            ScriptedTurn(
                tools=(("read_file", {"path": "a.txt"}),),
                usage=_USAGE_TURNS[0],
            ),
            ScriptedTurn(text="Done.", usage=_USAGE_TURNS[1]),
        ),
        grade=_grade_usage_accumulates,
        tags=("runtime", "usage"),
    ),
    Task(
        id="tool-raises-single",
        description="A tool that raises becomes an error result and the loop continues",
        prompt="Run the exploding tool.",
        permission_mode="full_auto",
        extra_tools={"explode": ExplodeTool()},
        script=(
            ScriptedTurn(tools=(("explode", {}),)),
            ScriptedTurn(text="The tool failed, but the turn completed."),
        ),
        grade=_grade_tool_raises_single,
        tags=("runtime", "tools", "robustness"),
    ),
    Task(
        id="tool-raises-in-batch",
        description="A raising tool in a parallel batch keeps the successful call intact",
        prompt="Run both tools.",
        fixture={"a.txt": "alpha\n"},
        permission_mode="full_auto",
        extra_tools={"explode": ExplodeTool()},
        script=(
            ScriptedTurn(
                tools=(
                    ("explode", {}),
                    ("read_file", {"path": "a.txt"}),
                ),
            ),
            ScriptedTurn(text="One call failed and one succeeded."),
        ),
        grade=_grade_tool_raises_in_batch,
        tags=("runtime", "tools", "robustness"),
    ),
    Task(
        id="interrupt-cancellation",
        description="Closing the stream mid-tool reports an interrupted turn and no half result",
        prompt="Read a.txt.",
        fixture={"a.txt": "alpha\n"},
        interrupt_after_tool_starts=1,
        script=(
            ScriptedTurn(tools=(("read_file", {"path": "a.txt"}),)),
            ScriptedTurn(text="This turn must never be reached."),
        ),
        grade=_grade_interrupt_cancellation,
        tags=("runtime", "interrupt"),
    ),
    Task(
        id="bash-timeout",
        description="A command exceeding its timeout is killed and reported as an error",
        prompt="Run a slow command.",
        permission_mode="full_auto",
        timeout_seconds=30.0,
        script=(
            ScriptedTurn(
                tools=(("bash", {"command": _TIMEOUT_COMMAND, "timeout_seconds": 2}),),
            ),
            ScriptedTurn(text="The command was killed after the timeout."),
        ),
        grade=_grade_bash_timeout,
        tags=("runtime", "bash", "timeout"),
    ),
    Task(
        id="compaction-triggers",
        description="Auto-compaction fires once, does not consume a scripted turn, and finishes",
        prompt="Read the files one by one.",
        fixture={
            "s1.txt": "x\n",
            "s2.txt": "x\n",
            "s3.txt": "x\n",
            # Below the 4,000-char microcompact floor but large enough that the
            # turn after this read crosses the 500-token threshold.
            "big.txt": "abcdefghij" * 240 + "\n",
        },
        permission_mode="full_auto",
        auto_compact_threshold_tokens=500,
        script=(
            ScriptedTurn(tools=(("read_file", {"path": "s1.txt"}),)),
            ScriptedTurn(tools=(("read_file", {"path": "s2.txt"}),)),
            ScriptedTurn(tools=(("read_file", {"path": "s3.txt"}),)),
            ScriptedTurn(tools=(("read_file", {"path": "big.txt"}),)),
            ScriptedTurn(text=_COMPACTION_FINAL_TEXT),
        ),
        grade=_grade_compaction_triggers,
        tags=("runtime", "compaction"),
    ),
    Task(
        id="artifact-outside-data-dir",
        description="Offloaded tool output lands in the task data dir, not the host data dir",
        prompt="Print a large amount of text.",
        permission_mode="full_auto",
        script=(
            ScriptedTurn(tools=(("bash", {"command": _LARGE_OUTPUT_COMMAND}),)),
            ScriptedTurn(text="The large output was offloaded."),
        ),
        grade=_grade_artifact_isolation,
        tags=("runtime", "tools", "artifacts"),
    ),
)

__all__ = ["CORE_TASKS"]
