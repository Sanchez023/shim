---
name: debug
description: >
  Diagnose and fix bugs, errors, test failures, and regressions. Use when the
  user reports a bug, an error or stack trace, unexpected behavior, or a failing
  test, or asks why a run failed, what changed between two runs, or why the
  output looks wrong or worse than before.
---

# debug

Find the root cause before changing code, then make the smallest fix that
addresses it.

## When to use

- The user reports a bug, error, stack trace, failing test, or unexpected behavior.
- The user asks "why did this fail?", "what changed?", "why does the output look
  wrong?", or "why is this run worse than the last one?"
- A command produced output that does not match what was expected.

## Workflow

1. **Reproduce** — get the exact steps, command, or input that triggers the issue.
2. **Read the evidence first** — the error text, stack trace, log lines, exit code.
   Read the message before searching the code; it usually names the failing line.
3. **Locate** — use Grep/Read to walk from the error to the relevant code path.
4. **Hypothesize, then verify** — form one theory and confirm it with a log line,
   a test, or a read of the surrounding code. Do not edit on a guess.
5. **Fix the root cause** — make the minimal change that addresses it, not the symptom.
6. **Prove it** — reproduce the original failure, then show it passes. Add a
   regression test when the bug could come back.

## Locating the failure layer

When the failure is in an agent or pipeline run rather than one function call,
localize the layer before explaining why:

- **Routing** — the wrong task type, profile, or model was selected.
- **Execution** — a tool call errored, was blocked by a permission, or returned
  something unexpected.
- **Verification** — output was produced but did not match the expected result.
- **Governance** — a safety or boundary check halted the run.

Read whatever run record the project archives — a manifest, an event log, a
failure signature, a trace file. If no artifacts exist, say so plainly and ask
for a trace or a re-run with capture enabled; do not reconstruct a story from
memory.

## Comparing runs

When a previous run passed and the current one does not, diff the two on input,
configuration, trace, and output. Report what actually differs, not what
plausibly could.

## Rules

- Evidence before intuition: read the error or the trace before forming a theory.
- Localize before explaining: name the stage that failed before describing why.
- Cite the source of every claim — `file:line`, trace line number, manifest field.
- Fix the root cause, not the symptom.
- Don't retry the same approach that already failed; investigate why it failed.
- If stuck after three attempts, report what you tried and what you ruled out.
