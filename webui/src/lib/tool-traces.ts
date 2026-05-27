import type { ToolProgressEvent } from "@/lib/types";

/** Drop duplicate tool_call objects (same id or identical formatted trace). */
export function dedupeToolCallsForUi(calls: unknown): unknown[] {
  if (!Array.isArray(calls) || calls.length === 0) return [];
  const seen = new Set<string>();
  const out: unknown[] = [];
  for (const c of calls) {
    let key: string | null = null;
    if (c && typeof c === "object" && "id" in c) {
      const id = (c as { id?: unknown }).id;
      if (typeof id === "string" && id.length > 0) key = `id:${id}`;
    }
    if (key == null) {
      key = formatToolCallTrace(c) ?? "";
    }
    if (!key || seen.has(key)) continue;
    seen.add(key);
    out.push(c);
  }
  return out;
}

export function formatToolCallTrace(call: unknown): string | null {
  if (!call || typeof call !== "object") return null;
  const item = call as {
    name?: unknown;
    arguments?: unknown;
    function?: { name?: unknown; arguments?: unknown };
  };
  const name =
    typeof item.function?.name === "string"
      ? item.function.name
      : typeof item.name === "string"
        ? item.name
        : "";
  if (!name) return null;
  const args = item.function?.arguments ?? item.arguments;
  if (typeof args === "string" && args.trim()) return `${name}(${args})`;
  if (args && typeof args === "object") return `${name}(${JSON.stringify(args)})`;
  return `${name}()`;
}

export type ToolTraceCategory =
  | "read"
  | "edit"
  | "check"
  | "search"
  | "shell"
  | "tool";

export type ToolTraceStatus = "queued" | "running" | "passed" | "failed" | "unknown";

export interface ParsedToolTrace {
  raw: string;
  name: string;
  args: unknown;
  category: ToolTraceCategory;
  status: ToolTraceStatus;
  target?: string;
  command?: string;
  callId?: string;
  checkpointId?: string;
  phase?: string;
  elapsedMs?: number;
  durationMs?: number;
  batchId?: string;
  batchIndex?: number;
  batchCount?: number;
  batchSize?: number;
  concurrencyLimit?: number;
  queuePosition?: number;
  summary?: string;
  riskCategory?: string;
  riskLevel?: string;
  blocked?: boolean;
  failureCategory?: string;
  recoveryAction?: string;
  retryable?: boolean;
  needsUserInput?: boolean;
  readOnly?: boolean;
  concurrencySafe?: boolean;
  exclusive?: boolean;
  configKey?: string;
  scopes?: string[];
}

const READ_TOOLS = new Set(["read_file", "list_dir", "grep"]);
const EDIT_TOOLS = new Set(["write_file", "edit_file", "notebook_edit"]);
const SEARCH_TOOLS = new Set(["web_search", "web_fetch", "search"]);
const SHELL_TOOLS = new Set(["exec", "shell"]);

const CHECK_COMMAND_RE =
  /\b(pytest|ruff\s+check|npm\s+run\s+(?:build|test|lint)|bun\s+run\s+(?:build|test|lint)|pnpm\s+(?:build|test|lint)|yarn\s+(?:build|test|lint)|cargo\s+test|go\s+test)\b/i;

export function parseToolTraceLine(line: string): ParsedToolTrace {
  const raw = line.trim();
  const match = /^([A-Za-z_][\w.-]*)\(([\s\S]*)\)$/.exec(raw);
  const name = match?.[1] ?? raw.split(/\s+/, 1)[0] ?? "tool";
  const argText = match?.[2] ?? "";
  const args = parseArgs(argText);
  const command = stringArg(args, "command") ?? stringArg(args, "cmd");
  const target =
    stringArg(args, "path")
    ?? stringArg(args, "query")
    ?? stringArg(args, "url")
    ?? stringArg(args, "pattern")
    ?? command;
  const category = classifyToolTrace(name, command);
  return { raw, name, args, category, target, command, status: "unknown" };
}

export function parseToolTraceEvent(event: ToolProgressEvent): ParsedToolTrace | null {
  const raw = formatToolCallTrace(event);
  if (!raw) return null;
  const name = typeof event.name === "string" && event.name ? event.name : "tool";
  const args = event.arguments && typeof event.arguments === "object" ? event.arguments : {};
  const command = stringArg(args, "command") ?? stringArg(args, "cmd");
  const target =
    stringArg(args, "path")
    ?? stringArg(args, "query")
    ?? stringArg(args, "url")
    ?? stringArg(args, "pattern")
    ?? command;
  const category = classifyToolTrace(name, command);
  const errorSummary = summarizeToolValue(event.error);
  const resultSummary = summarizeToolValue(event.result, category === "check");
  const status = inferToolStatus(event.phase, event.result, event.error);
  const safety = event.safety && typeof event.safety === "object" ? event.safety : {};
  const riskCategory =
    typeof event.risk_category === "string"
      ? event.risk_category
      : typeof safety.category === "string"
        ? safety.category
        : category;
  const riskLevel =
    typeof event.risk_level === "string"
      ? event.risk_level
      : typeof safety.level === "string"
        ? safety.level
        : undefined;
  const blocked =
    safety.blocked === true
    || event.failure_category === "safety_block"
    || isBlockedToolError(event.error);
  return {
    raw,
    name,
    args,
    category,
    target,
    command,
    status,
    callId: typeof event.call_id === "string" && event.call_id ? event.call_id : undefined,
    checkpointId:
      typeof event.checkpoint_id === "string" && event.checkpoint_id
        ? event.checkpoint_id
        : undefined,
    phase: typeof event.phase === "string" ? event.phase : undefined,
    elapsedMs: typeof event.elapsed_ms === "number" ? event.elapsed_ms : undefined,
    durationMs: typeof event.duration_ms === "number" ? event.duration_ms : undefined,
    batchId: typeof event.batch_id === "string" ? event.batch_id : undefined,
    batchIndex: typeof event.batch_index === "number" ? event.batch_index : undefined,
    batchCount: typeof event.batch_count === "number" ? event.batch_count : undefined,
    batchSize: typeof event.batch_size === "number" ? event.batch_size : undefined,
    concurrencyLimit: typeof event.concurrency_limit === "number" ? event.concurrency_limit : undefined,
    queuePosition: typeof event.queue_position === "number" ? event.queue_position : undefined,
    summary: errorSummary ?? resultSummary,
    riskCategory,
    riskLevel,
    blocked,
    failureCategory:
      typeof event.failure_category === "string" ? event.failure_category : undefined,
    recoveryAction:
      typeof event.recovery_action === "string" ? event.recovery_action : undefined,
    retryable: typeof event.retryable === "boolean" ? event.retryable : undefined,
    needsUserInput:
      typeof event.needs_user_input === "boolean" ? event.needs_user_input : undefined,
    readOnly: typeof event.read_only === "boolean" ? event.read_only : undefined,
    concurrencySafe:
      typeof event.concurrency_safe === "boolean" ? event.concurrency_safe : undefined,
    exclusive: typeof event.exclusive === "boolean" ? event.exclusive : undefined,
    configKey: typeof event.config_key === "string" && event.config_key ? event.config_key : undefined,
    scopes: Array.isArray(event.scopes)
      ? event.scopes.filter((scope): scope is string => typeof scope === "string")
      : undefined,
  };
}

export function compactToolTraceLabel(trace: ParsedToolTrace): string {
  const target = trace.command ?? trace.target;
  if (!target) return trace.name;
  return `${trace.name} ${shortenTarget(target)}`;
}

export function isCheckCommand(command: string | undefined): boolean {
  return !!command && CHECK_COMMAND_RE.test(command);
}

function classifyToolTrace(name: string, command: string | undefined): ToolTraceCategory {
  if (EDIT_TOOLS.has(name)) return "edit";
  if (READ_TOOLS.has(name)) return "read";
  if (SEARCH_TOOLS.has(name)) return "search";
  if (SHELL_TOOLS.has(name)) return isCheckCommand(command) ? "check" : "shell";
  return "tool";
}

export function toolProgressEventsFromEvents(events: unknown): ToolProgressEvent[] {
  if (!Array.isArray(events)) return [];
  const out: ToolProgressEvent[] = [];
  for (const event of events) {
    const normalized = normalizeToolProgressEvent(event);
    if (normalized) out.push(normalized);
  }
  return out;
}

export function mergeUniqueToolProgressEvents(
  previousEvents: ToolProgressEvent[] | undefined,
  incomingEvents: ToolProgressEvent[],
): { events: ToolProgressEvent[]; changed: boolean } {
  const events = [...(previousEvents ?? [])];
  const indexByKey = new Map<string, number>();
  events.forEach((event, index) => {
    const key = toolEventKey(event);
    if (key) indexByKey.set(key, index);
  });

  let changed = false;
  for (const event of incomingEvents) {
    const key = toolEventKey(event);
    if (!key) {
      events.push(event);
      changed = true;
      continue;
    }
    const existingIndex = indexByKey.get(key);
    if (existingIndex === undefined) {
      indexByKey.set(key, events.length);
      events.push(event);
      changed = true;
      continue;
    }
    const existing = events[existingIndex];
    if (JSON.stringify(existing) !== JSON.stringify(event)) {
      events[existingIndex] = { ...existing, ...event };
      changed = true;
    }
  }
  return { events, changed };
}

function normalizeToolProgressEvent(event: unknown): ToolProgressEvent | null {
  if (!event || typeof event !== "object") return null;
  const row = event as Record<string, unknown>;
  const phase = row.phase;
  if (!(phase && typeof phase === "string" && VALID_PHASES.has(phase))) return null;
  const normalized: ToolProgressEvent = {
    version: typeof row.version === "number" ? row.version : undefined,
    phase,
    checkpoint_id: typeof row.checkpoint_id === "string" ? row.checkpoint_id : undefined,
    call_id: typeof row.call_id === "string" ? row.call_id : undefined,
    name: typeof row.name === "string" ? row.name : undefined,
    arguments: row.arguments,
    result: row.result,
    error: row.error,
    risk_category: typeof row.risk_category === "string" ? row.risk_category : undefined,
    risk_level: typeof row.risk_level === "string" ? row.risk_level : undefined,
    safety: row.safety && typeof row.safety === "object"
      ? row.safety as ToolProgressEvent["safety"]
      : undefined,
    files: Array.isArray(row.files) ? row.files : undefined,
    embeds: Array.isArray(row.embeds) ? row.embeds : undefined,
    queued_at: typeof row.queued_at === "string" ? row.queued_at : undefined,
    started_at: typeof row.started_at === "string" ? row.started_at : undefined,
    completed_at: typeof row.completed_at === "string" ? row.completed_at : undefined,
    elapsed_ms: typeof row.elapsed_ms === "number" ? row.elapsed_ms : undefined,
    duration_ms: typeof row.duration_ms === "number" ? row.duration_ms : undefined,
    batch_id: typeof row.batch_id === "string" ? row.batch_id : undefined,
    batch_index: typeof row.batch_index === "number" ? row.batch_index : undefined,
    batch_count: typeof row.batch_count === "number" ? row.batch_count : undefined,
    batch_size: typeof row.batch_size === "number" ? row.batch_size : undefined,
    concurrency_limit: typeof row.concurrency_limit === "number" ? row.concurrency_limit : undefined,
    queue_position: typeof row.queue_position === "number" ? row.queue_position : undefined,
    failure_category: typeof row.failure_category === "string" ? row.failure_category : undefined,
    recovery_action: typeof row.recovery_action === "string" ? row.recovery_action : undefined,
    retryable: typeof row.retryable === "boolean" ? row.retryable : undefined,
    needs_user_input: typeof row.needs_user_input === "boolean" ? row.needs_user_input : undefined,
    read_only: typeof row.read_only === "boolean" ? row.read_only : undefined,
    concurrency_safe: typeof row.concurrency_safe === "boolean" ? row.concurrency_safe : undefined,
    exclusive: typeof row.exclusive === "boolean" ? row.exclusive : undefined,
    config_key: typeof row.config_key === "string" ? row.config_key : undefined,
    scopes: Array.isArray(row.scopes)
      ? row.scopes.filter((scope): scope is string => typeof scope === "string")
      : undefined,
  };
  return formatToolCallTrace(normalized) ? normalized : null;
}

function toolEventKey(event: ToolProgressEvent): string | null {
  if (event.call_id) return `id:${event.call_id}`;
  const trace = formatToolCallTrace(event);
  return trace ? `trace:${trace}` : null;
}

function inferToolStatus(
  phase: string | undefined,
  result: unknown,
  error: unknown,
): ToolTraceStatus {
  if (summarizeToolValue(error)) return "failed";
  if (phase === "error") return "failed";
  if (phase === "queued") return "queued";
  if (phase === "start" || phase === "running") return "running";
  if (phase === "end") return resultIndicatesFailure(result) ? "failed" : "passed";
  return "unknown";
}

function resultIndicatesFailure(result: unknown): boolean {
  if (result && typeof result === "object" && !Array.isArray(result)) {
    const row = result as Record<string, unknown>;
    const code = row.exit_code ?? row.exitCode ?? row.returncode ?? row.returnCode ?? row.code;
    if (typeof code === "number") return code !== 0;
    const status = row.status;
    if (typeof status === "string" && /^(error|failed|failure)$/i.test(status)) return true;
  }
  if (typeof result !== "string") return false;
  const text = result.trim();
  const exit = /exit code:\s*(-?\d+)/i.exec(text);
  if (exit) return Number(exit[1]) !== 0;
  if (/\b[1-9]\d*\s+failed\b/i.test(text)) return true;
  if (/\b(error|errors|failed|failure|traceback|exception)\b/i.test(text)) {
    return !/\b0\s+(failed|errors?)\b/i.test(text);
  }
  return false;
}

function summarizeToolValue(value: unknown, preferCheckLine = false): string | undefined {
  if (value == null) return undefined;
  if (typeof value === "object") {
    try {
      return shortenTarget(JSON.stringify(value));
    } catch {
      return undefined;
    }
  }
  const text = String(value).replace(/\r/g, "").trim();
  if (!text) return undefined;
  const lines = text.split("\n").map((line) => line.trim()).filter(Boolean);
  if (preferCheckLine) {
    const checkLine = lines.find((line) =>
      /\b(\d+\s+(?:passed|failed|errors?|skipped)|exit code:\s*-?\d+|built in|compiled|failed to compile)\b/i
        .test(line),
    );
    if (checkLine) return shortenTarget(checkLine);
  }
  return shortenTarget(lines[0] ?? text);
}

function isBlockedToolError(error: unknown): boolean {
  if (typeof error !== "string") return false;
  return /command blocked|blocked by|safety guard|deny pattern|allowlist/i.test(error);
}

function parseArgs(argText: string): unknown {
  const trimmed = argText.trim();
  if (!trimmed) return {};
  try {
    return JSON.parse(trimmed);
  } catch {
    return trimmed;
  }
}

function stringArg(args: unknown, key: string): string | undefined {
  if (!args || typeof args !== "object" || Array.isArray(args)) return undefined;
  const value = (args as Record<string, unknown>)[key];
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

function shortenTarget(value: string): string {
  const normalized = value.replace(/\s+/g, " ").trim();
  if (normalized.length <= 80) return normalized;
  return `${normalized.slice(0, 38)}...${normalized.slice(-32)}`;
}

const VALID_PHASES = new Set(["queued", "start", "running", "end", "error"]);

export function toolTraceLinesFromEvents(events: unknown): string[] {
  if (!Array.isArray(events)) return [];
  const seen = new Set<string>();
  const lines: string[] = [];
  for (const event of events) {
    if (!event || typeof event !== "object") continue;
    const phase = (event as { phase?: unknown }).phase;
    if (!(phase && typeof phase === "string" && VALID_PHASES.has(phase))) continue;
    const callId = (event as { call_id?: unknown }).call_id;
    if (callId && typeof callId === "string") {
      if (seen.has(callId)) continue;
      seen.add(callId);
    }
    const line = formatToolCallTrace(event);
    if (!line) continue;
    lines.push(line);
  }
  return lines;
}

export function mergeUniqueToolTraceLines(
  previousTraces: string[],
  lines: string[],
): { traces: string[]; added: boolean } {
  const seen = new Set(previousTraces);
  const traces = [...previousTraces];
  let added = false;
  for (const line of lines) {
    if (seen.has(line)) continue;
    seen.add(line);
    traces.push(line);
    added = true;
  }
  return { traces, added };
}
