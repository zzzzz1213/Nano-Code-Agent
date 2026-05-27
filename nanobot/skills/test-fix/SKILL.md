---
name: test-fix
description: "Use for test/build failures: inspect the failing signal, apply the smallest fix, and rerun focused checks."
metadata: {"nanobot":{"task_keywords":["test failed","tests failed","pytest","vitest","npm test","bun test","build failed","测试失败","单测失败","报错","traceback"],"priority":75}}
---

# Test Fix

Use this skill when the task centers on a failing test, build, lint, traceback, or command error.

## Failure Workflow

1. Preserve the failing signal.
   - Identify the exact command, test name, and shortest useful error line.
   - Avoid dumping large logs into memory or summaries.

2. Repair minimally.
   - Read the code path under test before editing.
   - Prefer fixing the behavior over weakening the test.
   - Keep unrelated refactors out of the change.

3. Recheck narrowly.
   - Rerun the failing test or fastest relevant command first.
   - Broaden checks only when the touched behavior is shared.
