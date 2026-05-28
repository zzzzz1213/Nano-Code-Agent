---
name: frontend-implementation
description: "Use for UI implementation tasks: build or refine React/Vue pages, components, styles, layout, and responsive behavior with minimal disruption."
metadata: {"nanobot":{"task_keywords":["frontend","ui","react component","tsx","css","responsive","layout","页面","前端","组件","样式","响应式"],"priority":70}}
---

# Frontend Implementation

Use this skill when the task is to build, refine, or debug a product-facing UI.

## Frontend Workflow

1. Preserve the existing design language.
   - Reuse current component patterns, spacing, tokens, and motion where possible.
   - Keep changes scoped to the page or component the user asked for.

2. Implement from structure to polish.
   - Read the current component tree and data flow before editing.
   - Fix layout, state, and styling in the smallest useful order.
   - Avoid mixing unrelated refactors into the same change.

3. Verify the visible behavior.
   - Check responsive states, empty states, and error states when they are relevant.
   - Prefer focused frontend tests or the fastest build/lint step available.
