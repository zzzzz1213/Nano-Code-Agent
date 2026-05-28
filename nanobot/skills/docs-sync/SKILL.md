---
name: docs-sync
description: "Use for documentation synchronization tasks: align README, changelog, setup notes, and developer docs with the actual code change."
metadata: {"nanobot":{"task_keywords":["docs sync","update docs","documentation","readme","changelog","docs","文档同步","更新文档","README","CHANGELOG","说明文档"],"priority":62}}
---

# Docs Sync

Use this skill when the task includes keeping documentation aligned with code or behavior changes.

## Documentation Workflow

1. Sync docs to real behavior.
   - Read the changed code path before editing docs.
   - Prefer precise statements over generic product language.

2. Update the right artifacts.
   - Touch README, setup notes, changelog, API docs, or local project docs only when they are actually affected.
   - Keep examples and commands consistent with the repository.

3. Leave the next reader oriented.
   - Mention user-visible behavior changes, setup implications, and any remaining caveats.
   - Avoid rewriting unrelated sections for style only.
