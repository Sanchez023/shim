---
name: plan
description: >
  Design an implementation plan before writing code. Use when the user asks to
  plan, design, or architect a feature or refactor, asks how to approach a
  change, or wants the steps and files laid out before implementation starts.
---

# plan

Design an implementation plan before coding.

## When to use

Use when the user asks to plan, design, or architect a feature before
implementing it, or asks how a change should be approached.

## Workflow

1. Understand the requirement:
   - What problem does this solve?
   - What are the constraints?
   - What's the expected outcome?
2. Explore the codebase to find:
   - Existing functions/utilities that can be reused
   - Patterns used elsewhere in the project
   - Files that will need modification
3. Design the approach:
   - Break into discrete steps
   - Identify dependencies between steps
   - Consider edge cases and error handling
4. Present the plan:
   - Start with a Context section (why this change)
   - List concrete steps with file paths
   - Include a verification section (how to test)

## Rules

- Read before suggesting — never propose changes to code you haven't read
- Prefer editing existing files over creating new ones
- Reuse existing patterns and utilities
- Include file paths and line numbers when referencing code
- Don't over-plan: match complexity to the task
