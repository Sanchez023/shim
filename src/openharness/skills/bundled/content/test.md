---
name: test
description: >
  Write, run, and verify tests. Use when the user asks for tests, wants coverage
  for a new feature or a bug fix, needs a regression test, or wants the test
  suite run and its results checked.
---

# test

Write and run tests for code.

## When to use

Use when the user asks to write tests, add coverage, add a regression test, or
run the test suite and verify the results.

## Workflow

1. Understand what needs testing:
   - New feature? Write unit tests for the happy path and edge cases
   - Bug fix? Write a regression test that would have caught the bug
   - Refactor? Ensure existing tests still pass
2. Follow the project's testing patterns:
   - Check existing tests for framework (pytest, jest, go test, etc.)
   - Match naming conventions and file organization
   - Use the same fixtures and helpers
3. Write tests that are:
   - Independent: each test can run alone
   - Deterministic: same result every time
   - Fast: mock external services, use in-memory databases
4. Run the tests and verify they pass

## Rules

- Test behavior, not implementation details
- One assertion per test when possible
- Use descriptive test names that explain the scenario
- Don't test framework or library code
- Mock at system boundaries (external APIs, filesystem, network)
