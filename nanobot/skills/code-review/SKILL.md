---
name: code-review
description: "Use for engineering review tasks: identify bugs, regressions, safety risks, and missing focused tests."
metadata: {"nanobot":{"task_keywords":["review","code review","审查","代码审查","风险","regression","回归","bug","安全"],"priority":80,"conflicts_with":["test-fix"]}}
---

# Code Review

Use this skill when the user asks for a review, risk check, regression analysis, or bug-focused inspection.

## Review Focus

1. Lead with findings.
   - Prioritize bugs, behavioral regressions, safety issues, and missing tests.
   - Cite exact files and lines when possible.
   - Keep summaries secondary to actionable findings.

2. Preserve project scope.
   - Review the changed behavior and nearby contracts.
   - Do not propose broad rewrites unless a defect requires it.
   - Avoid exposing secrets, raw credentials, or unnecessary logs.

3. Verify like an engineering assistant.
   - Prefer focused tests that cover the risky behavior.
   - Mention any check that was not run.
