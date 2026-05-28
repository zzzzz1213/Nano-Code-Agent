---
name: dependency-upgrade
description: "Use for dependency and version upgrade tasks: inspect changelog risk, update packages minimally, and verify compatibility."
metadata: {"nanobot":{"task_keywords":["dependency upgrade","upgrade dependency","bump version","package upgrade","version upgrade","npm install","pip install","依赖升级","升级依赖","版本升级","升级包"],"priority":72,"conflicts_with":["migration-planning"]}}
---

# Dependency Upgrade

Use this skill when the task centers on updating packages, SDKs, runtimes, or lockfiles.

## Upgrade Workflow

1. Upgrade with context.
   - Check the currently pinned version, upgrade target, and direct dependents.
   - Watch for breaking changes, config changes, and generated file churn.

2. Keep the change narrow.
   - Update the smallest necessary dependency set first.
   - Avoid bundling unrelated refactors with the upgrade.

3. Re-verify the affected surface.
   - Prefer focused tests, builds, or lint checks that exercise the upgraded package.
   - Call out any unverified downstream risk explicitly.
