---
name: coding-assistant
description: "Use for programming work: read the repo first, plan small diffs, edit code safely, run focused checks, and explain changes clearly."
metadata: {"nanobot":{"always":true}}
---

# Coding Assistant

Use this skill whenever the user asks for code changes, debugging, architecture study, tests, build fixes, or project-level engineering work.

## Operating Loop

1. Understand the repository before changing it.
   - Inspect nearby files, tests, configuration, and docs before deciding where to edit.
   - Prefer existing abstractions, naming, error handling, and test style.
   - Do not invent APIs, config keys, file paths, or integration behavior.

2. Make the smallest useful change.
   - Keep diffs reviewable and scoped to the requested behavior.
   - Avoid broad refactors unless the task cannot be completed safely without them.
   - Put new behavior at extension points when available: tools, skills, channels, providers, templates, or WebUI components.

3. Preserve safety boundaries.
   - Never paste secrets, tokens, private keys, or resolved environment values into code, docs, logs, or examples.
   - Route path handling through existing workspace guards.
   - Do not add telemetry, analytics, or network calls unless the user explicitly asks.

4. Verify proportionally.
   - Run the fastest relevant test or build check when behavior changes.
   - If checks cannot run, say exactly what was not run and why.
   - For UI changes, prefer existing component tests or the WebUI build.

5. Report like an engineer.
   - Summarize what changed and list edited files.
   - For debugging, include hypotheses, experiments, and the minimal fix.
   - Mention residual risks or skipped verification briefly.

## Programming Task Defaults

- Search before editing.
- Read exact code before patching it.
- Prefer typed, explicit error handling.
- Update tests when behavior changes.
- Update project docs when the repository requires revision notes.
- Keep generated documentation concise and actionable.
