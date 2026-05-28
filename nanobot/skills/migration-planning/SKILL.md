---
name: migration-planning
description: "Use for migration and rollout planning tasks: schema changes, framework upgrades, compatibility strategy, and staged cutovers."
metadata: {"nanobot":{"task_keywords":["migration","migrate","rollout","compatibility","breaking change","schema migration","迁移","迁移规划","升级方案","兼容","分阶段"],"priority":68,"conflicts_with":["dependency-upgrade"]}}
---

# Migration Planning

Use this skill when the task involves planning or sequencing a risky technical change.

## Planning Workflow

1. Identify the boundary of change.
   - Clarify what system, API, schema, dependency, or workflow is changing.
   - Note compatibility expectations, data impact, and rollback constraints.

2. Break the migration into safe steps.
   - Prefer phased rollout, compatibility shims, and reversible checkpoints.
   - Call out prerequisites, data backfills, and validation points.

3. Keep the plan actionable.
   - Produce concrete steps, risk areas, and verification points.
   - Avoid vague “rewrite everything” guidance unless the user explicitly wants that.
