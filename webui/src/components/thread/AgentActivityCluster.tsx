import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import {
  AlertCircle,
  Archive,
  CheckCircle2,
  ChevronRight,
  Code2,
  FileSearch,
  Layers,
  PencilLine,
  Save,
  Search,
  TerminalSquare,
  Wrench,
  Zap,
} from "lucide-react";
import { Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import { FileReferenceChip } from "@/components/FileReferenceChip";
import {
  AlertDialog,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
  AlertDialogCancel,
} from "@/components/ui/alert-dialog";
import {
  ReasoningBubble,
  StreamingLabelSheen,
} from "@/components/MessageBubble";
import { applyRecoveryAction, commitMemoryCandidate, recoverMemory } from "@/lib/api";
import {
  compactToolTraceLabel,
  parseToolTraceEvent,
  parseToolTraceLine,
  type ParsedToolTrace,
  type ToolTraceCategory,
  type ToolTraceStatus,
} from "@/lib/tool-traces";
import { cn } from "@/lib/utils";
import type {
  UIContextCompaction,
  UIActiveSkills,
  UIFileEdit,
  UIMemoryCandidate,
  UIMessage,
  UIMemorySnapshot,
  UIMemorySourceStats,
  UITurnCheckpoint,
} from "@/lib/types";
import { useOptionalClient } from "@/providers/ClientProvider";

/** Scrollport height for the Cursor-style “live trace” strip (tailwind spacing). */
const CLUSTER_SCROLL_MAX_CLASS = "max-h-52";
const ACTIVITY_SCROLL_NEAR_BOTTOM_PX = 24;

export function isReasoningOnlyAssistant(m: UIMessage): boolean {
  if (m.role !== "assistant" || m.kind === "trace") return false;
  if (m.content.trim().length > 0) return false;
  return !!(m.reasoning?.length || m.reasoningStreaming || m.isStreaming);
}

export function isAgentActivityMember(m: UIMessage): boolean {
  return isReasoningOnlyAssistant(m) || m.kind === "trace";
}

interface ActivityCounts {
  reasoningSteps: number;
  toolCalls: number;
  fileCount: number;
  added: number;
  deleted: number;
  hasEditingFiles: boolean;
  hasFailedFiles: boolean;
  primaryFilePath?: string;
  primaryFileTooltipPath?: string;
}

interface FileEditSummary {
  key: string;
  path: string;
  absolute_path?: string;
  added: number;
  deleted: number;
  approximate: boolean;
  binary: boolean;
  status: UIFileEdit["status"];
  changeKind: FileChangeKind;
  area: FileArea;
  extension: string;
  pending: boolean;
  error?: string;
}

type FileChangeKind = "added" | "modified" | "deleted";
type FileArea = "frontend" | "backend" | "test" | "docs" | "config" | "other";
type ActivityPhaseId = "read" | "tools" | "edit" | "check" | "done";
type ActivityPhaseStatus = "pending" | "running" | "done" | "failed";

interface ActivityPhase {
  id: ActivityPhaseId;
  label: string;
  detail: string;
  status: ActivityPhaseStatus;
}

type SnapshotCheckState = "none" | "running" | "passed" | "failed";
type SnapshotSource = "live" | "history" | "recovered";

interface TurnSnapshot {
  phaseLabel: string;
  phaseDetail: string;
  status: ActivityPhaseStatus;
  toolCount: number;
  fileCount: number;
  added: number;
  deleted: number;
  checkState: SnapshotCheckState;
  checkDetail: string;
  hasFailures: boolean;
  recoverable: boolean;
  reusedToolCount: number;
  compensationToolCount: number;
  retryableToolCount: number;
  requiresUserToolCount: number;
  resumableToolCount: number;
  safeResumeToolCount: number;
  reviewRequiredToolCount: number;
  needsInputToolCount: number;
  blockedToolCount: number;
  recoveryReviewItems: RecoveryReviewItem[];
  source: SnapshotSource;
  taskPlanItems: TaskPlanItem[];
  checkSummary: WorkbenchCheckSummary;
  diffPreview: WorkbenchDiffPreview;
  turnSummary: WorkbenchTurnSummary;
}

interface TaskPlanItem {
  key: string;
  label: string;
  detail: string;
  status: ActivityPhaseStatus;
  anchorId?: string;
  actionLabel?: string;
}

interface WorkbenchCheckSummary {
  total: number;
  running: number;
  passed: number;
  failed: number;
  primaryCommand?: string;
  failureSummary?: string;
  failureCategory?: string;
  diagnosticLabel?: string;
  diagnosticHint?: string;
  recommendedAction?: string;
  relatedTarget?: string;
}

interface WorkbenchDiffPreview {
  total: number;
  items: FileEditSummary[];
  binaryCount: number;
  approximateCount: number;
  failedCount: number;
  largeChangeCount: number;
}

interface WorkbenchTurnSummary {
  modifiedSummary: string;
  checksSummary: string;
  riskSummary: string;
  nextStep: string;
  nextStepAnchorId?: string;
  nextStepActionLabel?: string;
}

type RecoveryReviewGroup =
  | "safe_resume"
  | "review_required"
  | "needs_input"
  | "blocked"
  | string;

interface RecoveryReviewItem {
  toolCallId?: string;
  name: string;
  group: RecoveryReviewGroup;
  reason?: string;
  recoveryAction?: string;
  actionLabel?: string;
  reviewKind?: string;
  summary?: string;
  configKey?: string;
  scope?: string;
  canResumeNow?: boolean;
  canRetryNow?: boolean;
  reviewState?: string;
  statusLabel?: string;
  inputRequired?: boolean;
  inputPlaceholder?: string | null;
  reviewConfirmed?: boolean;
}

interface RecoveryReviewLocalState {
  reviewState: string;
  statusLabel: string;
}

const LARGE_DIFF_LINE_THRESHOLD = 200;

interface ToolSchedulingSummary {
  queued: number;
  running: number;
  completed: number;
  failed: number;
  total: number;
  concurrencyLimit?: number;
  batchCount?: number;
}

function countActivity(
  messages: UIMessage[],
  fileEdits: FileEditSummary[],
): ActivityCounts {
  let reasoningSteps = 0;
  let toolCalls = 0;
  for (const m of messages) {
    if (isReasoningOnlyAssistant(m)) {
      reasoningSteps += 1;
      continue;
    }
    if (m.kind === "trace") {
      const lines = m.traces?.length ?? (m.content.trim() ? 1 : 0);
      toolCalls += lines;
    }
  }
  let added = 0;
  let deleted = 0;
  let hasEditingFiles = false;
  let failedFileCount = 0;
  let primaryFilePath: string | undefined;
  let primaryFileTooltipPath: string | undefined;
  for (const edit of fileEdits) {
    primaryFilePath = edit.path;
    primaryFileTooltipPath = edit.absolute_path || edit.path;
    if (edit.status === "editing") {
      hasEditingFiles = true;
    }
    if (edit.status === "error") {
      failedFileCount += 1;
    }
    if (edit.status === "error" || edit.binary) {
      continue;
    }
    added += edit.added;
    deleted += edit.deleted;
  }
  return {
    reasoningSteps,
    toolCalls,
    fileCount: fileEdits.length,
    added,
    deleted,
    hasEditingFiles,
    hasFailedFiles:
      fileEdits.length > 0 && failedFileCount === fileEdits.length,
    primaryFilePath,
    primaryFileTooltipPath,
  };
}

interface AgentActivityClusterProps {
  messages: UIMessage[];
  /** True while the session turn is still running (drives “Working…” copy + header sheen). */
  isTurnStreaming: boolean;
  hasBodyBelow: boolean;
  sessionKey?: string | null;
  onResumeSafeTools?: () => void;
}

/**
 * Outer fold wrapping interleaved reasoning-only assistant rows and tool-trace rows.
 * Fixed max height with inner scroll; each block keeps its own small collapsible (reasoning / tools).
 */
export function AgentActivityCluster({
  messages,
  isTurnStreaming,
  hasBodyBelow,
  sessionKey = null,
  onResumeSafeTools,
}: AgentActivityClusterProps) {
  const { t } = useTranslation();
  const fileEdits = useMemo(
    () => summarizeFileEdits(collectFileEdits(messages), isTurnStreaming),
    [messages, isTurnStreaming],
  );
  const toolTraces = useMemo(() => collectToolTraces(messages), [messages]);
  const contextCompactions = useMemo(
    () => collectContextCompactions(messages),
    [messages],
  );
  const memorySnapshots = useMemo(
    () => collectMemorySnapshots(messages),
    [messages],
  );
  const activeSkills = useMemo(
    () => collectActiveSkills(messages),
    [messages],
  );
  const memoryCandidates = useMemo(
    () => collectMemoryCandidates(messages),
    [messages],
  );
  const {
    reasoningSteps,
    toolCalls,
    fileCount,
    added,
    deleted,
    hasEditingFiles,
    hasFailedFiles,
    primaryFilePath,
    primaryFileTooltipPath,
  } = countActivity(messages, fileEdits);
  const hasPendingFileEdit = fileEdits.some((edit) => edit.pending);
  const checkTraces = toolTraces.filter((trace) => trace.category === "check");
  const hasCheckTraces = checkTraces.length > 0;
  const hasReadTraces = toolTraces.some((trace) => trace.category === "read");
  const failedCheckCount = checkTraces.filter(
    (trace) => trace.status === "failed",
  ).length;
  const passedCheckCount = checkTraces.filter(
    (trace) => trace.status === "passed",
  ).length;
  const runningCheckCount = checkTraces.filter(
    (trace) => trace.status === "running",
  ).length;
  const checkpoint = useMemo(
    () => collectLatestCheckpoint(messages),
    [messages],
  );
  const activityPhases = useMemo(
    () =>
      buildActivityPhases({
        t,
        isTurnStreaming,
        toolTraces,
        fileEdits,
        reasoningSteps,
        hasEditingFiles,
        hasFailedFiles,
        failedCheckCount,
        passedCheckCount,
        runningCheckCount,
      }),
    [
      t,
      isTurnStreaming,
      toolTraces,
      fileEdits,
      reasoningSteps,
      hasEditingFiles,
      hasFailedFiles,
      failedCheckCount,
      passedCheckCount,
      runningCheckCount,
    ],
  );
  const currentPhase = currentActivityPhase(activityPhases);
  const turnSnapshot = useMemo(
    () =>
      buildTurnSnapshot({
        currentPhase,
        activityPhases,
        toolTraces,
        fileEdits,
        added,
        deleted,
        failedCheckCount,
        passedCheckCount,
        runningCheckCount,
        isTurnStreaming,
        checkpoint,
      }),
    [
      currentPhase,
      activityPhases,
      toolTraces,
      fileEdits,
      added,
      deleted,
      failedCheckCount,
      passedCheckCount,
      runningCheckCount,
      isTurnStreaming,
      checkpoint,
    ],
  );

  const [userToggledOuter, setUserToggledOuter] = useState(false);
  const [outerOpenLocal, setOuterOpenLocal] = useState(false);
  const activityScrollRef = useRef<HTMLDivElement>(null);
  const activityContentRef = useRef<HTMLDivElement>(null);
  const autoFollowActivityRef = useRef(true);
  const scrollFrameRef = useRef<number | null>(null);
  /** Collapsed by default during “Working…” and after the turn; user expands to inspect traces. */
  const outerExpanded = userToggledOuter ? outerOpenLocal : false;

  const hasLiveEditingFiles = isTurnStreaming && hasEditingFiles;
  const headerBusy = fileCount > 0 ? hasEditingFiles : isTurnStreaming;
  const singleFilePath = fileCount === 1 ? primaryFilePath : undefined;
  const singleFileTooltipPath =
    fileCount === 1 ? primaryFileTooltipPath : undefined;

  const fileActivitySummary =
    fileCount > 0
      ? hasPendingFileEdit && !singleFilePath
        ? t("message.fileActivityPreparing", {
            defaultValue: "Preparing edit…",
          })
        : singleFilePath
          ? t(fileActivitySummaryKey(hasLiveEditingFiles, hasFailedFiles), {
              file: shortFileName(singleFilePath),
              defaultValue: `${fileActivityVerb(hasLiveEditingFiles, hasFailedFiles)} {{file}}`,
            })
          : t(fileActivityManySummaryKey(hasLiveEditingFiles, hasFailedFiles), {
              count: fileCount,
              defaultValue: fileChangeSummaryLabel(
                fileEdits,
                hasLiveEditingFiles,
                hasFailedFiles,
              ),
            })
      : "";

  const detailSummary =
    fileCount > 0
      ? fileActivitySummary
      : hasCheckTraces
        ? checkActivitySummary({
            t,
            isTurnStreaming,
            failed: failedCheckCount,
            passed: passedCheckCount,
            running: runningCheckCount,
            total: checkTraces.length,
          })
        : hasReadTraces
          ? t("message.engineeringActivity.reading", {
              count: toolTraces.filter((trace) => trace.category === "read")
                .length,
              defaultValue: isTurnStreaming
                ? "Reading project…"
                : "Read project context",
            })
          : contextCompactions.length > 0
            ? t("message.engineeringActivity.contextCompressed", {
                count: contextCompactions.length,
                defaultValue:
                  contextCompactions.length === 1
                    ? "Context compressed"
                    : "Context compressed · {{count}} events",
              })
            : memoryCandidates.length > 0
              ? t("message.engineeringActivity.memoryCandidate", {
                  defaultValue: "Memory candidate",
                })
              : activeSkills.length > 0
                ? t("message.engineeringActivity.activeSkills", {
                    defaultValue: "Active skills",
                  })
                : memorySnapshots.length > 0
                  ? t("message.engineeringActivity.memorySnapshot", {
                      defaultValue: "Memory snapshot",
                    })
                  : isTurnStreaming
                    ? reasoningSteps > 0
                      ? t("message.agentActivityLiveSummary", {
                          reasoning: reasoningSteps,
                          tools: toolCalls,
                          defaultValue:
                            "Working… · {{reasoning}} steps · {{tools}} tool calls",
                        })
                      : toolCalls === 0 && fileCount > 0
                        ? t("message.agentActivityLiveFilesOnly", {
                            defaultValue: "Working…",
                          })
                        : t("message.agentActivityLiveToolsOnly", {
                            tools: toolCalls,
                            defaultValue: "Working… · {{tools}} tool calls",
                          })
                    : reasoningSteps > 0
                      ? t("message.agentActivitySummary", {
                          reasoning: reasoningSteps,
                          tools: toolCalls,
                          defaultValue:
                            "{{reasoning}} steps · {{tools}} tool calls",
                        })
                      : toolCalls === 0 && fileCount > 0
                        ? t("message.agentActivityFilesOnly", {
                            defaultValue: "File changes",
                          })
                        : t("message.agentActivityToolsOnly", {
                            tools: toolCalls,
                            defaultValue: "{{tools}} tool calls",
                          });
  const summary = currentPhase
    ? `${currentPhase.label} · ${currentPhase.detail || detailSummary}`
    : detailSummary;

  const cancelActivityScrollFrame = useCallback(() => {
    if (scrollFrameRef.current !== null) {
      window.cancelAnimationFrame(scrollFrameRef.current);
      scrollFrameRef.current = null;
    }
  }, []);

  const scrollActivityToBottom = useCallback(() => {
    const el = activityScrollRef.current;
    if (!el) return;
    el.scrollTop = Math.max(0, el.scrollHeight - el.clientHeight);
  }, []);

  const scheduleActivityScrollToBottom = useCallback(() => {
    cancelActivityScrollFrame();
    scrollFrameRef.current = window.requestAnimationFrame(() => {
      scrollFrameRef.current = null;
      scrollActivityToBottom();
    });
  }, [cancelActivityScrollFrame, scrollActivityToBottom]);

  const toggleOuter = () => {
    const nextOpen = userToggledOuter ? !outerOpenLocal : !outerExpanded;
    if (nextOpen) {
      autoFollowActivityRef.current = true;
    }
    setUserToggledOuter(true);
    setOuterOpenLocal(nextOpen);
  };

  useLayoutEffect(() => {
    if (!outerExpanded || !autoFollowActivityRef.current) return;
    scheduleActivityScrollToBottom();
  }, [
    outerExpanded,
    messages,
    isTurnStreaming,
    scheduleActivityScrollToBottom,
  ]);

  useEffect(() => {
    if (!outerExpanded) {
      autoFollowActivityRef.current = true;
      return;
    }
    const target = activityContentRef.current;
    if (!target || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => {
      if (autoFollowActivityRef.current) {
        scheduleActivityScrollToBottom();
      }
    });
    observer.observe(target);
    return () => observer.disconnect();
  }, [outerExpanded, scheduleActivityScrollToBottom]);

  useEffect(() => cancelActivityScrollFrame, [cancelActivityScrollFrame]);

  const onActivityScroll = useCallback(() => {
    const el = activityScrollRef.current;
    if (!el) return;
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
    autoFollowActivityRef.current = distance < ACTIVITY_SCROLL_NEAR_BOTTOM_PX;
  }, []);

  return (
    <div className={cn("w-full", hasBodyBelow && "mb-2")}>
      <button
        type="button"
        onClick={toggleOuter}
        className={cn(
          "group flex w-full items-center gap-2 rounded-md px-2 py-1.5",
          "text-xs text-muted-foreground transition-colors hover:bg-muted/45",
        )}
        aria-expanded={outerExpanded}
        aria-label={summary}
      >
        <Layers className="h-3.5 w-3.5 shrink-0" aria-hidden />
        <span className="flex min-w-0 flex-1 flex-wrap items-center gap-x-1.5 gap-y-0.5 text-left">
          {singleFilePath && !currentPhase ? (
            <span className="inline-flex min-w-0 items-center gap-1.5">
              <StreamingLabelSheen active={headerBusy} className="shrink-0">
                {fileActivityVerb(hasLiveEditingFiles, hasFailedFiles)}
              </StreamingLabelSheen>
              <FileReferenceChip
                path={singleFilePath}
                tooltipPath={singleFileTooltipPath}
                active={hasLiveEditingFiles}
                className="-my-0.5 min-w-0"
                textClassName="text-xs"
                testId="activity-header-file-reference"
              />
            </span>
          ) : (
            <StreamingLabelSheen active={headerBusy} className="min-w-0">
              {summary}
            </StreamingLabelSheen>
          )}
          {currentPhase && singleFilePath ? (
            <FileReferenceChip
              path={singleFilePath}
              tooltipPath={singleFileTooltipPath}
              active={hasLiveEditingFiles}
              className="-my-0.5 min-w-0"
              textClassName="text-xs"
              testId="activity-header-file-reference"
            />
          ) : null}
          {fileCount > 0 && (
            <span className="inline-flex min-w-0 items-center gap-1 text-muted-foreground/85">
              <DiffPair added={added} deleted={deleted} />
            </span>
          )}
        </span>
        <ChevronRight
          aria-hidden
          className={cn(
            "h-3.5 w-3.5 shrink-0 transition-transform duration-200",
            outerExpanded && "rotate-90",
          )}
        />
      </button>

      {outerExpanded && (
        <div
          className={cn(
            "mt-1 overflow-hidden rounded-md border border-border/50 bg-muted/25",
          )}
        >
          <div
            ref={activityScrollRef}
            data-testid="agent-activity-scroll"
            onScroll={onActivityScroll}
            className={cn(
              CLUSTER_SCROLL_MAX_CLASS,
              "overflow-y-auto px-2 py-1.5 scrollbar-thin scrollbar-track-transparent",
            )}
          >
            <div ref={activityContentRef} className="flex flex-col gap-2">
              {activityPhases.length ? (
                <ActivityTimeline phases={activityPhases} />
              ) : null}
              {contextCompactions.length ? (
                <ContextCompactionGroup compactions={contextCompactions} />
              ) : null}
              {/** Show retrieved memories if they were injected into compaction summaries */}
              {contextCompactions.length ? (
                <RetrievedMemoriesGroup compactions={contextCompactions} />
              ) : null}
              {memorySnapshots.length ? (
                <MemorySnapshotGroup snapshots={memorySnapshots} />
              ) : null}
              {activeSkills.length ? (
                <ActiveSkillsGroup snapshots={activeSkills} />
              ) : null}
              {memoryCandidates.length ? (
                <MemoryCandidateGroup candidates={memoryCandidates} />
              ) : null}
                {turnSnapshot ? (
                  <TurnSnapshotPanel
                    snapshot={turnSnapshot}
                    sessionKey={sessionKey}
                    onResumeSafeTools={onResumeSafeTools}
                  />
                ) : null}
              {messages.map((m) => {
                if (isReasoningOnlyAssistant(m)) {
                  return (
                    <ReasoningBubble
                      key={m.id}
                      text={m.reasoning ?? ""}
                      streaming={isTurnStreaming && !!m.reasoningStreaming}
                      hasBodyBelow={false}
                      embeddedInCluster
                    />
                  );
                }
                return null;
              })}
              {toolTraces.length ? (
                <EngineeringTraceSections
                  traces={toolTraces}
                  isTurnStreaming={isTurnStreaming}
                />
              ) : null}
              {fileEdits.length ? <FileEditGroup edits={fileEdits} /> : null}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function shortFileName(path: string): string {
  return path.split(/[\\/]/).pop() || path;
}

function fileActivityVerb(editing: boolean, failed: boolean): string {
  if (failed) return "Failed";
  return editing ? "Editing" : "Edited";
}

function fileActivitySummaryKey(editing: boolean, failed: boolean): string {
  if (failed) return "message.fileActivityFailedOne";
  return editing
    ? "message.fileActivityEditingOne"
    : "message.fileActivityEditedOne";
}

function fileActivityManySummaryKey(editing: boolean, failed: boolean): string {
  if (failed) return "message.fileActivityFailedMany";
  return editing
    ? "message.fileActivityEditingMany"
    : "message.fileActivityEditedMany";
}

function fileEditCallKey(edit: UIFileEdit): string {
  if (edit.call_id) return `${edit.call_id}|${edit.tool}`;
  return `${edit.tool}|${edit.path}`;
}

function collectFileEdits(messages: UIMessage[]): UIFileEdit[] {
  const edits: UIFileEdit[] = [];
  for (const message of messages) {
    if (message.kind === "trace" && message.fileEdits?.length) {
      edits.push(...message.fileEdits);
    }
  }
  return edits;
}

function collectToolTraces(messages: UIMessage[]): ParsedToolTrace[] {
  const traces: ParsedToolTrace[] = [];
  const seen = new Set<string>();
  for (const message of messages) {
    if (message.kind !== "trace") continue;
    if (message.toolEvents?.length) {
      for (const event of message.toolEvents) {
        const parsed = parseToolTraceEvent(event);
        if (!parsed) continue;
        const key = parsed.callId ? `id:${parsed.callId}` : parsed.raw;
        if (!parsed.raw || seen.has(key)) continue;
        seen.add(key);
        seen.add(parsed.raw);
        traces.push(parsed);
      }
    }
    const lines = message.traces?.length
      ? message.traces
      : message.content.trim()
        ? [message.content]
        : [];
    for (const line of lines) {
      const parsed = parseToolTraceLine(line);
      if (!parsed.raw || seen.has(parsed.raw)) continue;
      seen.add(parsed.raw);
      traces.push(parsed);
    }
  }
  return traces;
}

function collectLatestCheckpoint(
  messages: UIMessage[],
): UITurnCheckpoint | undefined {
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const checkpoint = messages[i].checkpoint;
    if (checkpoint) return checkpoint;
  }
  return undefined;
}

function collectContextCompactions(
  messages: UIMessage[],
): UIContextCompaction[] {
  const events: UIContextCompaction[] = [];
  for (const message of messages) {
    if (message.kind === "trace" && message.contextCompaction) {
      events.push(message.contextCompaction);
    }
  }
  return events;
}

function collectMemorySnapshots(messages: UIMessage[]): UIMemorySnapshot[] {
  const snapshots: UIMemorySnapshot[] = [];
  for (const message of messages) {
    if (message.kind === "trace" && message.memorySnapshot) {
      snapshots.push(message.memorySnapshot);
    }
  }
  return snapshots;
}

function collectActiveSkills(messages: UIMessage[]): UIActiveSkills[] {
  const snapshots: UIActiveSkills[] = [];
  for (const message of messages) {
    if (message.kind === "trace" && message.activeSkills) {
      snapshots.push(message.activeSkills);
    }
  }
  return snapshots;
}

function collectMemoryCandidates(messages: UIMessage[]): UIMemoryCandidate[] {
  const candidates: UIMemoryCandidate[] = [];
  for (const message of messages) {
    if (message.kind === "trace" && message.memoryCandidate) {
      candidates.push(message.memoryCandidate);
    }
  }
  return candidates;
}

function checkActivitySummary({
  t,
  isTurnStreaming,
  failed,
  passed,
  running,
  total,
}: {
  t: ReturnType<typeof useTranslation>["t"];
  isTurnStreaming: boolean;
  failed: number;
  passed: number;
  running: number;
  total: number;
}) {
  if (failed > 0) {
    return t("message.engineeringActivity.checkFailed", {
      count: failed,
      defaultValue: failed === 1 ? "1 check failed" : "{{count}} checks failed",
    });
  }
  if (running > 0 || isTurnStreaming) {
    return t("message.engineeringActivity.checking", {
      count: running || total,
      defaultValue: "Running checks...",
    });
  }
  if (passed > 0 && passed === total) {
    return t("message.engineeringActivity.checkPassed", {
      count: passed,
      defaultValue: passed === 1 ? "1 check passed" : "{{count}} checks passed",
    });
  }
  return t("message.engineeringActivity.checked", {
    count: total,
    defaultValue: "Checks complete",
  });
}

function buildActivityPhases({
  t,
  isTurnStreaming,
  toolTraces,
  fileEdits,
  reasoningSteps,
  hasEditingFiles,
  hasFailedFiles,
  failedCheckCount,
  passedCheckCount,
  runningCheckCount,
}: {
  t: ReturnType<typeof useTranslation>["t"];
  isTurnStreaming: boolean;
  toolTraces: ParsedToolTrace[];
  fileEdits: FileEditSummary[];
  reasoningSteps: number;
  hasEditingFiles: boolean;
  hasFailedFiles: boolean;
  failedCheckCount: number;
  passedCheckCount: number;
  runningCheckCount: number;
}): ActivityPhase[] {
  const readCount = toolTraces.filter(
    (trace) => trace.category === "read",
  ).length;
  const checkCount = toolTraces.filter(
    (trace) => trace.category === "check",
  ).length;
  const utilityTraces = toolTraces.filter(
    (trace) => !["read", "check", "edit"].includes(trace.category),
  );
  const failedUtilityCount = utilityTraces.filter(
    (trace) => trace.status === "failed",
  ).length;
  const hasAnyActivity =
    reasoningSteps > 0 || toolTraces.length > 0 || fileEdits.length > 0;
  if (!hasAnyActivity) return [];
  const hasPendingFileEdit = fileEdits.some((edit) => edit.pending);

  const hasLaterThanRead =
    utilityTraces.length > 0 || fileEdits.length > 0 || checkCount > 0;
  const hasLaterThanTools = fileEdits.length > 0 || checkCount > 0;
  const hasLaterThanEdit = checkCount > 0;
  const checksFailed = failedCheckCount > 0;
  const checksRunning =
    runningCheckCount > 0 ||
    (isTurnStreaming && checkCount > 0 && !checksFailed);
  const checksDone = checkCount > 0 && !checksFailed && !checksRunning;
  const allKnownWorkDone =
    !isTurnStreaming &&
    !hasFailedFiles &&
    !checksFailed &&
    (fileEdits.length > 0 || checkCount > 0 || toolTraces.length > 0);

  return [
    {
      id: "read",
      label: t("message.engineeringActivity.timeline.read", {
        defaultValue: "Reading",
      }),
      detail:
        readCount > 0
          ? t("message.engineeringActivity.timeline.readDetail", {
              count: readCount,
              defaultValue: readCount === 1 ? "1 source" : "{{count}} sources",
            })
          : t("message.engineeringActivity.timeline.pending", {
              defaultValue: "Pending",
            }),
      status:
        readCount > 0
          ? isTurnStreaming && !hasLaterThanRead
            ? "running"
            : "done"
          : isTurnStreaming && reasoningSteps > 0
            ? "running"
            : "pending",
    },
    {
      id: "tools",
      label: t("message.engineeringActivity.timeline.tools", {
        defaultValue: "Tools",
      }),
      detail:
        utilityTraces.length > 0
          ? t("message.engineeringActivity.timeline.toolsDetail", {
              count: utilityTraces.length,
              defaultValue:
                utilityTraces.length === 1 ? "1 step" : "{{count}} steps",
            })
          : t("message.engineeringActivity.timeline.pending", {
              defaultValue: "Pending",
            }),
      status:
        utilityTraces.length > 0
          ? failedUtilityCount > 0
            ? "failed"
            : isTurnStreaming && !hasLaterThanTools
              ? "running"
              : "done"
          : "pending",
    },
    {
      id: "edit",
      label: t("message.engineeringActivity.timeline.edit", {
        defaultValue: "Editing",
      }),
      detail:
        fileEdits.length > 0
          ? hasPendingFileEdit
            ? t("message.engineeringActivity.timeline.editPreparing", {
                defaultValue: "Preparing",
              })
            : t("message.engineeringActivity.timeline.editDetail", {
                count: fileEdits.length,
                defaultValue:
                  fileEdits.length === 1 ? "1 file" : "{{count}} files",
              })
          : t("message.engineeringActivity.timeline.pending", {
              defaultValue: "Pending",
            }),
      status:
        fileEdits.length > 0
          ? hasFailedFiles
            ? "failed"
            : hasEditingFiles || (isTurnStreaming && !hasLaterThanEdit)
              ? "running"
              : "done"
          : "pending",
    },
    {
      id: "check",
      label: t("message.engineeringActivity.timeline.check", {
        defaultValue: "Checking",
      }),
      detail:
        checkCount > 0
          ? checksFailed
            ? t("message.engineeringActivity.timeline.checkFailedDetail", {
                count: failedCheckCount,
                defaultValue:
                  failedCheckCount === 1 ? "1 failed" : "{{count}} failed",
              })
            : checksDone
              ? t("message.engineeringActivity.timeline.checkPassedDetail", {
                  count: passedCheckCount || checkCount,
                  defaultValue:
                    (passedCheckCount || checkCount) === 1
                      ? "1 passed"
                      : "{{count}} passed",
                })
              : t("message.engineeringActivity.timeline.checkRunningDetail", {
                  count: runningCheckCount || checkCount,
                  defaultValue: "Running",
                })
          : t("message.engineeringActivity.timeline.pending", {
              defaultValue: "Pending",
            }),
      status:
        checkCount > 0
          ? checksFailed
            ? "failed"
            : checksRunning
              ? "running"
              : "done"
          : "pending",
    },
    {
      id: "done",
      label: t("message.engineeringActivity.timeline.done", {
        defaultValue: "Done",
      }),
      detail: allKnownWorkDone
        ? t("message.engineeringActivity.timeline.doneDetail", {
            defaultValue: "Ready",
          })
        : t("message.engineeringActivity.timeline.pending", {
            defaultValue: "Pending",
          }),
      status: allKnownWorkDone
        ? "done"
        : checksFailed || hasFailedFiles
          ? "failed"
          : "pending",
    },
  ];
}

function currentActivityPhase(
  phases: ActivityPhase[],
): ActivityPhase | undefined {
  return (
    phases.find((phase) => phase.status === "failed") ??
    [...phases].reverse().find((phase) => phase.status === "running") ??
    [...phases]
      .reverse()
      .find((phase) => phase.status === "done" && phase.id !== "done") ??
    phases.find((phase) => phase.id === "done" && phase.status === "done")
  );
}

function buildTurnSnapshot({
  currentPhase,
  activityPhases,
  toolTraces,
  fileEdits,
  added,
  deleted,
  failedCheckCount,
  passedCheckCount,
  runningCheckCount,
  isTurnStreaming,
  checkpoint,
}: {
  currentPhase: ActivityPhase | undefined;
  activityPhases: ActivityPhase[];
  toolTraces: ParsedToolTrace[];
  fileEdits: FileEditSummary[];
  added: number;
  deleted: number;
  failedCheckCount: number;
  passedCheckCount: number;
  runningCheckCount: number;
  isTurnStreaming: boolean;
  checkpoint?: UITurnCheckpoint;
}): TurnSnapshot | null {
  if (!checkpoint && (!currentPhase || activityPhases.length === 0))
    return null;
  const checkCount = toolTraces.filter(
    (trace) => trace.category === "check",
  ).length;
  const inferredCheckState: SnapshotCheckState =
    failedCheckCount > 0
      ? "failed"
      : runningCheckCount > 0 || (isTurnStreaming && checkCount > 0)
        ? "running"
        : checkCount > 0
          ? "passed"
          : "none";
  const checkState =
    normalizeCheckpointCheckState(checkpoint?.check_state) ??
    inferredCheckState;
  const checkpointStatus = checkpoint
    ? checkpointPhaseStatus(checkpoint.phase, checkState, isTurnStreaming)
    : undefined;
  const fallbackPhase: ActivityPhase = currentPhase ?? {
    id: checkpointStatus === "done" ? "done" : "tools",
    label: checkpointPhaseLabel(checkpoint?.phase),
    detail: checkpointPhaseDetail(checkpoint?.phase),
    status: checkpointStatus ?? "pending",
  };
  const checkDetail =
    checkState === "failed"
      ? `${Math.max(1, failedCheckCount)} failed`
      : checkState === "running"
        ? "Running"
        : checkState === "passed"
          ? `${Math.max(1, passedCheckCount || checkCount)} passed`
          : "Not run";
  const hasFailures =
    checkState === "failed" ||
    failedCheckCount > 0 ||
    activityPhases.some((phase) => phase.status === "failed") ||
    fileEdits.some((edit) => edit.status === "error");
  const source = snapshotSource(checkpoint, isTurnStreaming);
  const toolCount = finiteCount(checkpoint?.tool_call_count, toolTraces.length);
  const fileCount = Math.max(
    finiteCount(checkpoint?.file_edit_count, fileEdits.length),
    fileEdits.length,
  );
  const recoveryReviewItems = normalizeRecoveryReviewItems(
    checkpoint?.recovery_review_items,
  );
  const checkSummary = buildWorkbenchCheckSummary({
    toolTraces,
    checkState,
    failedCheckCount,
    passedCheckCount,
    runningCheckCount,
  });
  const diffPreview = buildWorkbenchDiffPreview(fileEdits);
  return {
    phaseLabel: checkpoint
      ? checkpointPhaseLabel(checkpoint.phase)
      : fallbackPhase.label,
    phaseDetail: checkpoint
      ? checkpointPhaseDetail(checkpoint.phase)
      : fallbackPhase.detail,
    status: checkpointStatus ?? fallbackPhase.status,
    toolCount,
    fileCount,
    added,
    deleted,
    checkState,
    checkDetail,
    hasFailures,
    recoverable: source !== "live",
    reusedToolCount: finiteCount(checkpoint?.reused_tool_count, 0),
    compensationToolCount: finiteCount(checkpoint?.compensation_tool_count, 0),
    retryableToolCount: finiteCount(checkpoint?.retryable_tool_count, 0),
    requiresUserToolCount: finiteCount(checkpoint?.requires_user_tool_count, 0),
    resumableToolCount: finiteCount(checkpoint?.resumable_tool_count, 0),
    safeResumeToolCount: finiteCount(checkpoint?.safe_resume_tool_count, 0),
    reviewRequiredToolCount: finiteCount(
      checkpoint?.review_required_tool_count,
      0,
    ),
    needsInputToolCount: finiteCount(checkpoint?.needs_input_tool_count, 0),
    blockedToolCount: finiteCount(checkpoint?.blocked_tool_count, 0),
    recoveryReviewItems,
    source,
    taskPlanItems: buildTaskPlanItems({
      activityPhases,
      currentPhase: checkpointStatus ? undefined : currentPhase,
      hasFailures,
      recoveryReviewItems,
      checkState,
    }),
    checkSummary,
    diffPreview,
    turnSummary: buildWorkbenchTurnSummary({
      toolCount,
      fileCount,
      added,
      deleted,
      checkState,
      checkSummary,
      diffPreview,
      hasFailures,
      recoverable: source !== "live",
      safeResumeToolCount: finiteCount(checkpoint?.safe_resume_tool_count, 0),
      reviewRequiredToolCount: finiteCount(
        checkpoint?.review_required_tool_count,
        0,
      ),
      needsInputToolCount: finiteCount(checkpoint?.needs_input_tool_count, 0),
      blockedToolCount: finiteCount(checkpoint?.blocked_tool_count, 0),
      resumableToolCount: finiteCount(checkpoint?.resumable_tool_count, 0),
      phaseLabel: checkpoint
        ? checkpointPhaseLabel(checkpoint.phase)
        : fallbackPhase.label,
    }),
  };
}

function buildTaskPlanItems({
  activityPhases,
  currentPhase,
  hasFailures,
  recoveryReviewItems,
  checkState,
}: {
  activityPhases: ActivityPhase[];
  currentPhase?: ActivityPhase;
  hasFailures: boolean;
  recoveryReviewItems: RecoveryReviewItem[];
  checkState: SnapshotCheckState;
}): TaskPlanItem[] {
  const items: TaskPlanItem[] = activityPhases.map((phase) => ({
    key: phase.id,
    label: phase.label,
    detail: phase.detail,
    status:
      currentPhase && currentPhase.id === phase.id
        ? currentPhase.status
        : phase.status,
  }));
  if (recoveryReviewItems.length > 0) {
    items.push({
      key: "recovery-review",
      label: "Review recovery actions",
      detail: `${recoveryReviewItems.length} pending item${recoveryReviewItems.length === 1 ? "" : "s"}`,
      status: "pending",
      anchorId: "recovery-review",
      actionLabel: "Open review",
    });
  } else if (hasFailures && checkState === "failed") {
    items.push({
      key: "fix-checks",
      label: "Resolve failing checks",
      detail: "Inspect the latest failed command before continuing.",
      status: "pending",
      anchorId: "check-results",
      actionLabel: "Open checks",
    });
  }
  return items;
}

function buildWorkbenchCheckSummary({
  toolTraces,
  checkState,
  failedCheckCount,
  passedCheckCount,
  runningCheckCount,
}: {
  toolTraces: ParsedToolTrace[];
  checkState: SnapshotCheckState;
  failedCheckCount: number;
  passedCheckCount: number;
  runningCheckCount: number;
}): WorkbenchCheckSummary {
  const checkTraces = toolTraces.filter((trace) => trace.category === "check");
  const failingTrace = [...checkTraces]
    .reverse()
    .find((trace) => trace.status === "failed");
  const activeTrace = [...checkTraces]
    .reverse()
    .find((trace) => trace.status === "running" || trace.status === "passed");
  return {
    total: checkTraces.length,
    running: checkState === "running" ? Math.max(1, runningCheckCount || checkTraces.filter((trace) => trace.status === "running").length) : runningCheckCount,
    passed: checkState === "passed" ? Math.max(1, passedCheckCount || checkTraces.filter((trace) => trace.status === "passed").length) : passedCheckCount,
    failed: checkState === "failed" ? Math.max(1, failedCheckCount || checkTraces.filter((trace) => trace.status === "failed").length) : failedCheckCount,
    primaryCommand:
      failingTrace?.command ??
      activeTrace?.command ??
      checkTraces.at(-1)?.command,
    failureSummary:
      failingTrace?.summary ??
      (checkState === "failed" ? "Check failed; inspect the latest command output." : undefined),
    failureCategory: failingTrace?.failureCategory,
    diagnosticLabel: failingTrace?.diagnosticLabel,
    diagnosticHint: failingTrace?.diagnosticHint,
    recommendedAction: failingTrace?.recommendedAction,
    relatedTarget: failingTrace?.target,
  };
}

function buildWorkbenchDiffPreview(
  fileEdits: FileEditSummary[],
): WorkbenchDiffPreview {
  return {
    total: fileEdits.length,
    items: fileEdits.slice(0, 4),
    binaryCount: fileEdits.filter((edit) => edit.binary).length,
    approximateCount: fileEdits.filter((edit) => edit.approximate).length,
    failedCount: fileEdits.filter((edit) => edit.status === "error").length,
    largeChangeCount: fileEdits.filter(
      (edit) => edit.added + edit.deleted >= LARGE_DIFF_LINE_THRESHOLD,
    ).length,
  };
}

function buildWorkbenchTurnSummary({
  toolCount,
  fileCount,
  added,
  deleted,
  checkState,
  checkSummary,
  diffPreview,
  hasFailures,
  recoverable,
  safeResumeToolCount,
  reviewRequiredToolCount,
  needsInputToolCount,
  blockedToolCount,
  resumableToolCount,
  phaseLabel,
}: {
  toolCount: number;
  fileCount: number;
  added: number;
  deleted: number;
  checkState: SnapshotCheckState;
  checkSummary: WorkbenchCheckSummary;
  diffPreview: WorkbenchDiffPreview;
  hasFailures: boolean;
  recoverable: boolean;
  safeResumeToolCount: number;
  reviewRequiredToolCount: number;
  needsInputToolCount: number;
  blockedToolCount: number;
  resumableToolCount: number;
  phaseLabel: string;
}): WorkbenchTurnSummary {
  const modifiedSummary =
    fileCount > 0
      ? `Edited ${fileCount} file${fileCount === 1 ? "" : "s"} (+${added} -${deleted}).`
      : toolCount > 0
        ? `Used ${toolCount} tool call${toolCount === 1 ? "" : "s"} in ${phaseLabel.toLowerCase()}.`
        : "No file changes or tool calls recorded yet.";
  const checksSummary =
    checkState === "failed"
      ? `${Math.max(1, checkSummary.failed)} failed check${checkSummary.failed === 1 ? "" : "s"}${checkSummary.primaryCommand ? ` · ${checkSummary.primaryCommand}` : ""}.`
      : checkState === "running"
        ? `${Math.max(1, checkSummary.running)} check${checkSummary.running === 1 ? "" : "s"} still running.`
        : checkState === "passed"
          ? `${Math.max(1, checkSummary.passed)} passed check${checkSummary.passed === 1 ? "" : "s"} recorded.`
          : "No checks captured in this turn.";
  const riskSummary =
    blockedToolCount > 0
      ? `${blockedToolCount} blocked action${blockedToolCount === 1 ? "" : "s"} still need a safer request before recovery can continue.`
      : needsInputToolCount > 0
        ? `${needsInputToolCount} tool${needsInputToolCount === 1 ? "" : "s"} still need extra user input.`
        : reviewRequiredToolCount > 0
          ? `${reviewRequiredToolCount} tool${reviewRequiredToolCount === 1 ? "" : "s"} need confirmation before retry.`
          : hasFailures && checkState === "failed"
            ? "A failed check still blocks the turn."
            : diffPreview.failedCount > 0
              ? `${diffPreview.failedCount} file edit${diffPreview.failedCount === 1 ? "" : "s"} did not finish cleanly.`
              : recoverable && resumableToolCount > 0
                ? `${resumableToolCount} tool${resumableToolCount === 1 ? "" : "s"} can resume from the recovered checkpoint.`
                : recoverable && safeResumeToolCount > 0
                  ? `${safeResumeToolCount} safe tool${safeResumeToolCount === 1 ? "" : "s"} can resume without rerunning the turn.`
                  : hasFailures
                    ? "One or more steps need attention before closing the turn."
                    : "No blocking risk detected.";
  let nextStep = "Review the latest output and continue if needed.";
  let nextStepAnchorId: string | undefined;
  let nextStepActionLabel: string | undefined;
  if (blockedToolCount > 0) {
    nextStep =
      "Open Recovery review and revise the blocked request before retrying.";
    nextStepAnchorId = "recovery-review";
    nextStepActionLabel = "Open review";
  } else if (needsInputToolCount > 0) {
    nextStep =
      "Open Recovery review and provide the missing input before retrying tools.";
    nextStepAnchorId = "recovery-review";
    nextStepActionLabel = "Open review";
  } else if (reviewRequiredToolCount > 0) {
    nextStep =
      "Open Recovery review and confirm the pending tool retries.";
    nextStepAnchorId = "recovery-review";
    nextStepActionLabel = "Open review";
  } else if (checkState === "failed") {
    nextStep = "Inspect the failing check summary and rerun the focused command.";
    nextStepAnchorId = "check-results";
    nextStepActionLabel = "Open checks";
  } else if (recoverable && safeResumeToolCount > 0) {
    nextStep = "Resume safe tools or review the remaining recovery items.";
    nextStepAnchorId = "recovery-review";
    nextStepActionLabel = "Open review";
  } else if (recoverable && resumableToolCount > 0) {
    nextStep = "Review the recoverable tools and continue the restored turn.";
    nextStepAnchorId = "recovery-review";
    nextStepActionLabel = "Open review";
  } else if (diffPreview.total > 0) {
    nextStep =
      diffPreview.binaryCount > 0 || diffPreview.largeChangeCount > 0
        ? "Inspect the diff preview for binary or high-volume changes before closing the turn."
        : "Inspect the diff preview before closing the turn.";
    nextStepAnchorId = "diff-preview";
    nextStepActionLabel = "Open diff";
  } else if (!hasFailures) {
    nextStep = "Review the completed changes or move to the next task.";
  }
  return {
    modifiedSummary,
    checksSummary,
    riskSummary,
    nextStep,
    nextStepAnchorId,
    nextStepActionLabel,
  };
}

function normalizeRecoveryReviewItems(value: unknown): RecoveryReviewItem[] {
  if (!Array.isArray(value)) return [];
  const items: RecoveryReviewItem[] = [];
  for (const item of value) {
    if (!item || typeof item !== "object") continue;
    const record = item as Record<string, unknown>;
    const name =
      typeof record.name === "string" && record.name.trim()
        ? record.name.trim()
        : "tool";
    const group =
      typeof record.group === "string" && record.group.trim()
        ? record.group.trim()
        : "review_required";
    items.push({
      toolCallId:
        typeof record.tool_call_id === "string"
          ? record.tool_call_id
          : undefined,
      name,
      group,
      reason:
        typeof record.reason === "string" && record.reason.trim()
          ? record.reason.trim()
          : undefined,
      recoveryAction:
        typeof record.recovery_action === "string" &&
        record.recovery_action.trim()
          ? record.recovery_action.trim()
          : undefined,
      actionLabel:
        typeof record.action_label === "string" && record.action_label.trim()
          ? record.action_label.trim()
          : undefined,
      reviewKind:
        typeof record.review_kind === "string" && record.review_kind.trim()
          ? record.review_kind.trim()
          : undefined,
      summary:
        typeof record.summary === "string" && record.summary.trim()
          ? record.summary.trim()
          : undefined,
      configKey:
        typeof record.config_key === "string" && record.config_key.trim()
          ? record.config_key.trim()
          : undefined,
      scope:
        typeof record.scope === "string" && record.scope.trim()
          ? record.scope.trim()
          : undefined,
      canResumeNow: record.can_resume_now === true,
      canRetryNow: record.can_retry_now === true,
      reviewState:
        typeof record.review_state === "string" && record.review_state.trim()
          ? record.review_state.trim()
          : undefined,
      statusLabel:
        typeof record.status_label === "string" && record.status_label.trim()
          ? record.status_label.trim()
          : undefined,
      inputRequired: record.input_required === true,
      inputPlaceholder:
        typeof record.input_placeholder === "string" && record.input_placeholder.trim()
          ? record.input_placeholder.trim()
          : undefined,
      reviewConfirmed: record.review_confirmed === true,
    });
  }
  return items;
}

function finiteCount(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value)
    ? Math.max(0, Math.round(value))
    : fallback;
}

function normalizeCheckpointCheckState(
  value: unknown,
): SnapshotCheckState | undefined {
  return value === "none" ||
    value === "running" ||
    value === "passed" ||
    value === "failed"
    ? value
    : undefined;
}

function snapshotSource(
  checkpoint: UITurnCheckpoint | undefined,
  isTurnStreaming: boolean,
): SnapshotSource {
  if (checkpoint?.source === "recovered" || checkpoint?.recovered === true)
    return "recovered";
  return isTurnStreaming ? "live" : "history";
}

function checkpointPhaseLabel(phase: unknown): string {
  if (phase === "awaiting_tools" || phase === "tools_completed") return "Tools";
  if (phase === "final_response") return "Done";
  return "Checkpoint";
}

function checkpointPhaseDetail(phase: unknown): string {
  if (phase === "awaiting_tools") return "Awaiting tool results";
  if (phase === "tools_completed") return "Tool results saved";
  if (phase === "final_response") return "Final response";
  return "Runtime state saved";
}

function checkpointPhaseStatus(
  phase: unknown,
  checkState: SnapshotCheckState,
  isTurnStreaming: boolean,
): ActivityPhaseStatus {
  if (checkState === "failed") return "failed";
  if (phase === "awaiting_tools" || isTurnStreaming) return "running";
  if (phase === "tools_completed" || phase === "final_response") return "done";
  return "pending";
}

function latestFileEditEvents(edits: UIFileEdit[]): UIFileEdit[] {
  const order: string[] = [];
  const byKey = new Map<string, UIFileEdit>();
  for (const edit of edits) {
    const key = fileEditCallKey(edit);
    if (!byKey.has(key)) order.push(key);
    byKey.set(key, edit);
  }
  return order.map((key) => byKey.get(key)).filter(Boolean) as UIFileEdit[];
}

function summarizeFileEdits(
  edits: UIFileEdit[],
  active: boolean,
): FileEditSummary[] {
  interface MutableSummary {
    key: string;
    path: string;
    absolute_path?: string;
    added: number;
    deleted: number;
    approximate: boolean;
    binary: boolean;
    pending: boolean;
    hasSuccessfulChange: boolean;
    hasActiveEditing: boolean;
    hasFailed: boolean;
    error?: string;
  }

  const order: string[] = [];
  const byPath = new Map<string, MutableSummary>();
  for (const edit of latestFileEditEvents(edits)) {
    const key = edit.path || edit.call_id || edit.tool;
    let summary = byPath.get(key);
    if (!summary) {
      summary = {
        key,
        path: edit.path || "",
        absolute_path: edit.absolute_path,
        added: 0,
        deleted: 0,
        approximate: false,
        binary: false,
        pending: false,
        hasSuccessfulChange: false,
        hasActiveEditing: false,
        hasFailed: false,
      };
      byPath.set(key, summary);
      order.push(key);
    }

    if (edit.path && !summary.path) {
      summary.path = edit.path;
    }
    if (edit.absolute_path) {
      summary.absolute_path = edit.absolute_path;
    }
    summary.pending = summary.pending || !!edit.pending || !edit.path;
    if (active && edit.status === "editing") {
      summary.hasActiveEditing = true;
      summary.binary = summary.binary || !!edit.binary;
      summary.approximate = summary.approximate || !!edit.approximate;
      if (!edit.binary) {
        summary.added += edit.added;
        summary.deleted += edit.deleted;
      }
      continue;
    }

    if (edit.status === "error") {
      summary.hasFailed = true;
      summary.error = edit.error ?? summary.error;
      continue;
    }

    summary.hasSuccessfulChange = true;
    summary.binary = summary.binary || !!edit.binary;
    summary.approximate = active && (summary.approximate || !!edit.approximate);
    if (!edit.binary) {
      summary.added += edit.added;
      summary.deleted += edit.deleted;
    }
  }

  return order.map((key) => {
    const summary = byPath.get(key)!;
    const status: UIFileEdit["status"] = summary.hasActiveEditing
      ? "editing"
      : summary.hasSuccessfulChange
        ? "done"
        : summary.hasFailed
          ? "error"
          : "done";
    return {
      key: summary.key,
      path: summary.path,
      absolute_path: summary.absolute_path,
      added: summary.added,
      deleted: summary.deleted,
      approximate: summary.approximate,
      binary: summary.binary,
      status,
      changeKind: inferFileChangeKind(summary.added, summary.deleted),
      area: fileArea(summary.path),
      extension: fileExtension(summary.path),
      pending: summary.pending && !summary.path,
      error: summary.error,
    };
  });
}

function FileEditGroup({ edits }: { edits: FileEditSummary[] }) {
  if (edits.length === 0) return null;
  const groups = groupedFileEdits(edits);
  return (
    <ActivitySection
      titleKey="message.engineeringActivity.sections.files"
      defaultTitle="File changes"
      icon={<PencilLine className="h-3.5 w-3.5" aria-hidden />}
    >
      <div className="space-y-1.5">
        {groups.map((group) => (
          <div key={group.area} className="space-y-1">
            {groups.length > 1 ? (
              <div className="flex items-center gap-1.5 px-2 text-[10.5px] font-medium text-muted-foreground/65">
                <Code2 className="h-3 w-3" aria-hidden />
                <span>{fileAreaLabel(group.area)}</span>
                <span className="text-muted-foreground/45">·</span>
                <span>{group.edits.length}</span>
              </div>
            ) : null}
            <ul className="space-y-1">
              {group.edits.map((edit) => (
                <FileEditRow key={edit.key} edit={edit} />
              ))}
            </ul>
          </div>
        ))}
      </div>
    </ActivitySection>
  );
}

function ActivityTimeline({ phases }: { phases: ActivityPhase[] }) {
  return (
    <section
      aria-label="Engineering activity timeline"
      className="rounded-md border border-border/45 bg-background/45 px-2 py-2"
    >
      <ol className="grid gap-1.5 sm:grid-cols-5">
        {phases.map((phase) => (
          <li
            key={phase.id}
            className={cn(
              "min-w-0 rounded-md border px-2 py-1.5",
              phase.status === "pending" &&
                "border-border/35 bg-muted/25 text-muted-foreground/55",
              phase.status === "running" &&
                "border-sky-500/20 bg-sky-500/10 text-sky-700 dark:text-sky-300",
              phase.status === "done" &&
                "border-emerald-500/20 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300",
              phase.status === "failed" &&
                "border-destructive/20 bg-destructive/10 text-destructive",
            )}
          >
            <div className="flex min-w-0 items-center gap-1.5">
              <ActivityPhaseIcon status={phase.status} />
              <span className="truncate text-[11px] font-medium">
                {phase.label}
              </span>
            </div>
            <div className="mt-0.5 truncate pl-4 text-[10.5px] opacity-75">
              {phase.detail}
            </div>
          </li>
        ))}
      </ol>
    </section>
  );
}

function ContextCompactionGroup({
  compactions,
}: {
  compactions: UIContextCompaction[];
}) {
  if (!compactions.length) return null;
  return (
    <ActivitySection
      titleKey="message.engineeringActivity.sections.context"
      defaultTitle="Context memory"
      icon={<Archive className="h-3.5 w-3.5" aria-hidden />}
    >
      <div className="space-y-1.5">
        {compactions.map((compaction, index) => (
          <ContextCompactionRow
            key={`${compaction.updated_at ?? "context"}-${index}`}
            compaction={compaction}
          />
        ))}
      </div>
    </ActivitySection>
  );
}

function MemorySnapshotGroup({ snapshots }: { snapshots: UIMemorySnapshot[] }) {
  const latest = snapshots[snapshots.length - 1];
  if (!latest) return null;
  const sources = memorySnapshotSources(latest);
  const retrieved = latest.retrieved;
  const retrievedCount = finiteCount(retrieved?.entry_count, 0);
  const retrievedCategories = memoryRetrievedCategories(retrieved?.categories);
  const retrievedReasons = (retrieved?.reasons ?? []).filter(Boolean).slice(0, 3);
  const retrievedItems = (retrieved?.items ?? []).slice(0, 3);
  return (
    <ActivitySection
      titleKey="message.engineeringActivity.sections.memory"
      defaultTitle="Memory snapshot"
      icon={<Archive className="h-3.5 w-3.5" aria-hidden />}
    >
      <div className="rounded-md border border-indigo-500/20 bg-indigo-500/10 px-2 py-2 text-xs text-indigo-800 dark:text-indigo-200">
        <div className="flex min-w-0 items-center justify-between gap-2">
          <div className="flex min-w-0 items-center gap-1.5">
            <Archive className="h-3.5 w-3.5 shrink-0" aria-hidden />
            <span className="truncate font-medium">Memory sources loaded</span>
          </div>
          <span className="shrink-0 rounded-full border border-indigo-500/20 px-1.5 py-0.5 text-[10px] font-medium">
            {sources.filter((source) => source.stats.included).length} active
          </span>
        </div>
        <div className="mt-1.5 grid gap-1.5 sm:grid-cols-2">
          {sources.map((source) => (
            <MemorySourceChip
              key={source.key}
              label={source.label}
              stats={source.stats}
            />
          ))}
        </div>
        {retrieved?.included ? (
          <div className="mt-2 rounded-md border border-indigo-500/20 bg-background/45 px-2 py-1.5">
            <div className="flex min-w-0 items-center justify-between gap-2">
              <div className="flex min-w-0 items-center gap-1.5">
                <Search className="h-3.5 w-3.5 shrink-0" aria-hidden />
                <span className="truncate font-medium">Retrieved metadata</span>
              </div>
              <span className="shrink-0 rounded-full border border-indigo-500/20 px-1.5 py-0.5 text-[10px] font-medium">
                Retrieved {retrievedCount}
              </span>
            </div>
            {retrievedCategories.length ? (
              <div className="mt-1.5 flex flex-wrap gap-1">
                {retrievedCategories.map(({ category, count }) => (
                  <span
                    key={category}
                    className="rounded-full border border-indigo-500/20 px-1.5 py-0.5 text-[10px]"
                  >
                    {memoryCategoryLabel(category)} {count}
                  </span>
                ))}
              </div>
            ) : null}
            {retrievedReasons.length ? (
              <div className="mt-1 flex flex-wrap gap-1">
                {retrievedReasons.map((reason) => (
                  <span
                    key={reason}
                    className="max-w-full truncate rounded border border-border/40 bg-muted/20 px-1.5 py-0.5 text-[10px] text-muted-foreground"
                  >
                    {reason}
                  </span>
                ))}
              </div>
            ) : null}
            {retrievedItems.length ? (
              <ul className="mt-1.5 space-y-1">
                {retrievedItems.map((item, index) => (
                  <li
                    key={item.id ?? `${item.source ?? "memory"}-${index}`}
                    className="min-w-0 rounded border border-border/35 bg-muted/15 px-2 py-1"
                  >
                    <div className="flex min-w-0 items-center justify-between gap-2">
                      <span className="truncate text-[11px] font-medium">
                        {memoryCategoryLabel(item.category)}
                      </span>
                      {item.safety ? (
                        <span className="shrink-0 text-[10px] opacity-70">
                          {item.safety}
                        </span>
                      ) : null}
                    </div>
                    <div className="mt-0.5 truncate text-[10.5px] opacity-75">
                      {item.reason ?? "matched"} · {item.source ?? "memory"}
                    </div>
                  </li>
                ))}
              </ul>
            ) : null}
          </div>
        ) : null}
      </div>
    </ActivitySection>
  );
}

function ActiveSkillsGroup({ snapshots }: { snapshots: UIActiveSkills[] }) {
  const latest = snapshots[snapshots.length - 1];
  const skills = latest?.skills?.filter((skill) => skill.name) ?? [];
  if (!latest || !skills.length) return null;
  const autoCount = skills.filter((skill) => skill.source === "auto").length;
  return (
    <ActivitySection
      titleKey="message.engineeringActivity.sections.skills"
      defaultTitle="Active skills"
      icon={<Zap className="h-3.5 w-3.5" aria-hidden />}
    >
      <div className="rounded-md border border-emerald-500/20 bg-emerald-500/10 px-2 py-2 text-xs text-emerald-800 dark:text-emerald-200">
        <div className="flex min-w-0 items-center justify-between gap-2">
          <div className="flex min-w-0 items-center gap-1.5">
            <Zap className="h-3.5 w-3.5 shrink-0" aria-hidden />
            <span className="truncate font-medium">Active skills</span>
          </div>
          <span className="shrink-0 rounded-full border border-emerald-500/20 px-1.5 py-0.5 text-[10px] font-medium">
            {skills.length} loaded
          </span>
        </div>
        <div className="mt-1.5 flex flex-wrap gap-1">
          {skills.map((skill) => (
            <span
              key={`${skill.name}-${skill.source ?? "skill"}`}
              className="inline-flex max-w-full items-center gap-1 rounded-full border border-emerald-500/20 bg-background/45 px-1.5 py-0.5 text-[10px] font-medium"
              title={activeSkillTitle(skill)}
            >
              <span className="truncate">{skill.name}</span>
              <span className="shrink-0 opacity-65">
                {activeSkillSourceLabel(skill.source)}
              </span>
            </span>
          ))}
        </div>
        {autoCount > 0 ? (
          <div className="mt-1.5 grid gap-1.5 sm:grid-cols-2">
            {skills
              .filter((skill) => skill.source === "auto")
              .map((skill) => (
                <div
                  key={`reason-${skill.name}`}
                  className="min-w-0 rounded border border-emerald-500/20 bg-background/45 px-2 py-1"
                >
                  <div className="truncate text-[11px] font-medium">
                    {skill.name}
                  </div>
                  <div className="mt-0.5 truncate text-[10.5px] opacity-75">
                    {skill.reason || "matched current task"}
                  </div>
                </div>
              ))}
          </div>
        ) : null}
      </div>
    </ActivitySection>
  );
}

function MemoryCandidateGroup({
  candidates,
}: {
  candidates: UIMemoryCandidate[];
}) {
  const clientContext = useOptionalClient();
  const [statusById, setStatusById] = useState<
    Record<string, "idle" | "saving" | "saved" | "duplicate" | "error">
  >({});
  if (!candidates.length) return null;

  const onCommit = async (candidate: UIMemoryCandidate) => {
    const id = candidate.id || candidate.content || "candidate";
    if (!clientContext) return;
    setStatusById((prev) => ({ ...prev, [id]: "saving" }));
    try {
      const result = await commitMemoryCandidate(
        clientContext.token,
        candidate,
      );
      setStatusById((prev) => ({
        ...prev,
        [id]: result.duplicate ? "duplicate" : "saved",
      }));
    } catch {
      setStatusById((prev) => ({ ...prev, [id]: "error" }));
    }
  };

  return (
    <ActivitySection
      titleKey="message.engineeringActivity.sections.memoryCandidate"
      defaultTitle="Memory candidate"
      icon={<Archive className="h-3.5 w-3.5" aria-hidden />}
    >
      <div className="space-y-1.5">
        {candidates.map((candidate, index) => {
          const id = candidate.id || `${candidate.target ?? "memory"}-${index}`;
          const status = statusById[id] ?? "idle";
          const disabled =
            !clientContext ||
            status === "saving" ||
            status === "saved" ||
            !!candidate.sensitive ||
            !!candidate.duplicate;
          return (
            <div
              key={id}
              className="rounded-md border border-amber-500/25 bg-amber-500/10 px-2 py-2 text-xs text-amber-900 dark:text-amber-100"
            >
              <div className="flex min-w-0 items-center justify-between gap-2">
                <div className="flex min-w-0 items-center gap-1.5">
                  <Archive className="h-3.5 w-3.5 shrink-0" aria-hidden />
                  <span className="truncate font-medium">
                    {memoryCandidateTitle(candidate)}
                  </span>
                </div>
                <span className="shrink-0 rounded-full border border-amber-500/25 px-1.5 py-0.5 text-[10px] font-medium">
                  {candidate.target || "memory"}
                </span>
              </div>
              <p className="mt-1.5 line-clamp-3 text-[11px] leading-4">
                {candidate.content || "Empty memory candidate"}
              </p>
              <div className="mt-1.5 flex min-w-0 items-center justify-between gap-2">
                <span className="truncate text-[10.5px] opacity-75">
                  {memoryCandidateStatusLabel(
                    status,
                    candidate,
                    !!clientContext,
                  )}
                </span>
                <button
                  type="button"
                  className={cn(
                    "inline-flex shrink-0 items-center gap-1 rounded border px-1.5 py-1 text-[10.5px] font-medium transition-colors",
                    disabled
                      ? "border-border/40 bg-muted/25 text-muted-foreground/55"
                      : "border-amber-600/25 bg-background/65 hover:bg-background",
                  )}
                  disabled={disabled}
                  onClick={() => void onCommit(candidate)}
                  aria-label="Save memory candidate"
                >
                  <Save className="h-3 w-3" aria-hidden />
                  <span>
                    {status === "saved"
                      ? "Saved"
                      : status === "saving"
                        ? "Saving"
                        : "Save"}
                  </span>
                </button>
              </div>
            </div>
          );
        })}
      </div>
    </ActivitySection>
  );
}

function memoryCandidateTitle(candidate: UIMemoryCandidate): string {
  if (candidate.title) return candidate.title;
  if (candidate.type === "project_memory") return "Project memory";
  if (candidate.type === "assistant_style") return "Assistant style";
  return "User profile";
}

function memoryCandidateStatusLabel(
  status: "idle" | "saving" | "saved" | "duplicate" | "error",
  candidate: UIMemoryCandidate,
  hasClient: boolean,
): string {
  if (candidate.sensitive) return "Sensitive text blocked";
  if (candidate.duplicate || status === "duplicate") return "Already in memory";
  if (status === "saving") return "Writing to memory";
  if (status === "saved") return "Saved to long-term memory";
  if (status === "error") return "Could not save";
  if (!hasClient) return "Connect WebUI to save";
  return candidate.reason || "Review before saving";
}

function memorySnapshotSources(snapshot: UIMemorySnapshot): Array<{
  key: string;
  label: string;
  stats: UIMemorySourceStats;
}> {
  const sources = snapshot.sources ?? {};
  return [
    { key: "memory", label: "Project memory", stats: sources.memory ?? {} },
    { key: "user", label: "User profile", stats: sources.user ?? {} },
    { key: "soul", label: "Assistant style", stats: sources.soul ?? {} },
    {
      key: "recent_history",
      label: "Recent history",
      stats: sources.recent_history ?? {},
    },
    {
      key: "session_summary",
      label: "Session summary",
      stats: sources.session_summary ?? {},
    },
  ];
}

function memoryRetrievedCategories(
  categories: Record<string, number> | undefined,
): Array<{ category: string; count: number }> {
  return Object.entries(categories ?? {})
    .map(([category, count]) => ({
      category,
      count: finiteCount(count, 0),
    }))
    .filter((item) => item.category && item.count > 0)
    .sort((a, b) => b.count - a.count || a.category.localeCompare(b.category));
}

function memoryCategoryLabel(category: string | undefined): string {
  if (category === "project_fact") return "Project fact";
  if (category === "user_preference") return "User preference";
  if (category === "assistant_style") return "Assistant style";
  if (category === "decision") return "Decision";
  if (category === "failure") return "Failure";
  if (category === "command") return "Command";
  return category || "Memory";
}

function MemorySourceChip({
  label,
  stats,
}: {
  label: string;
  stats: UIMemorySourceStats;
}) {
  const active = !!stats.included;
  const detail =
    stats.entry_count !== undefined
      ? `${finiteCount(stats.entry_count, 0)} entries`
      : `${finiteCount(stats.token_estimate, 0)} tokens`;
  return (
    <div
      className={cn(
        "min-w-0 rounded border px-2 py-1.5",
        active
          ? "border-indigo-500/20 bg-indigo-500/10"
          : "border-border/35 bg-muted/20 text-muted-foreground/65",
      )}
    >
      <div className="flex min-w-0 items-center justify-between gap-1.5">
        <span className="truncate font-medium">{label}</span>
        <span className="shrink-0 text-[10px] uppercase tracking-[0.08em] opacity-70">
          {active ? "On" : "Off"}
        </span>
      </div>
      <div className="mt-0.5 truncate text-[10.5px] opacity-75">{detail}</div>
    </div>
  );
}

function ContextCompactionRow({
  compaction,
}: {
  compaction: UIContextCompaction;
}) {
  const reason = contextCompactionReasonLabel(compaction.reason);
  const saved = finiteCount(compaction.saved_token_estimate, 0);
  const before = finiteCount(compaction.before_token_estimate, 0);
  const after = finiteCount(compaction.after_token_estimate, 0);
  const archived = finiteCount(compaction.archived_message_count, 0);
  const kept = finiteCount(
    compaction.kept_message_count ?? compaction.after_message_count,
    0,
  );
  const structuredSummary = formatCompactionSummarySections(
    compaction.summary_sections,
  );
  return (
    <div className="rounded-md border border-teal-500/20 bg-teal-500/10 px-2 py-2 text-xs text-teal-800 dark:text-teal-200">
      <div className="flex min-w-0 items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-1.5">
          <Archive className="h-3.5 w-3.5 shrink-0" aria-hidden />
          <span className="truncate font-medium">Context compressed</span>
        </div>
        <span className="shrink-0 rounded-full border border-teal-500/20 px-1.5 py-0.5 text-[10px] font-medium">
          {reason}
        </span>
      </div>
      <div className="mt-1.5 grid gap-1.5 sm:grid-cols-3">
        <SnapshotMetric
          label="Tokens"
          value={saved > 0 ? `-${saved}` : `${before} -> ${after}`}
        />
        <SnapshotMetric
          label="Messages"
          value={`${archived} archived · ${kept} kept`}
        />
        <SnapshotMetric
          label="Summary"
          value={`${finiteCount(compaction.summary_token_estimate, 0)} tokens`}
        />
      </div>
      {compaction.summary_preview ? (
        <p className="mt-1.5 line-clamp-2 text-[11px] leading-4 text-teal-900/75 dark:text-teal-100/75">
          {compaction.summary_preview}
        </p>
      ) : null}
      {structuredSummary ? (
        <p className="mt-1.5 line-clamp-3 text-[11px] leading-4 text-teal-900/70 dark:text-teal-100/70">
          {structuredSummary}
        </p>
      ) : null}
      {compaction.summary_full ? (
        <div className="mt-2 flex items-center justify-end">
          <AlertDialog>
            <AlertDialogTrigger asChild>
              <button
                type="button"
                className="inline-flex items-center gap-2 rounded border px-2 py-1 text-[11px] font-medium"
              >
                View full summary
                <ChevronRight className="h-3 w-3" aria-hidden />
              </button>
            </AlertDialogTrigger>
            <AlertDialogContent>
              <AlertDialogHeader>
                <AlertDialogTitle>Structured summary</AlertDialogTitle>
                <AlertDialogDescription>
                  Full structured summary generated by the consolidator.
                </AlertDialogDescription>
              </AlertDialogHeader>
              <div className="prose max-h-[60vh] overflow-auto mt-2">
                <pre className="whitespace-pre-wrap">
                  {compaction.summary_full}
                </pre>
              </div>
              <AlertDialogFooter>
                <AlertDialogCancel>Close</AlertDialogCancel>
              </AlertDialogFooter>
            </AlertDialogContent>
          </AlertDialog>
        </div>
      ) : null}
    </div>
  );
}

function formatCompactionSummarySections(
  sections?: Record<string, string[]>,
): string {
  if (!sections) return "";
  const order = [
    "overview",
    "goal",
    "constraints",
    "files_touched",
    "commands_run",
    "failures",
    "decisions",
    "next_steps",
  ];
  const labels: Record<string, string> = {
    overview: "Overview",
    goal: "Goal",
    constraints: "Constraints",
    files_touched: "Files touched",
    commands_run: "Commands run",
    failures: "Failures",
    decisions: "Decisions",
    next_steps: "Next steps",
  };
  const parts: string[] = [];
  for (const key of order) {
    const values = sections[key];
    if (!values?.length) continue;
    parts.push(`${labels[key]}: ${values[0]}`);
  }
  return parts.join(" · ");
}

/** Parse retrieved memories block from a compaction.summary_full text.
 * Looks for a header line starting with "[Retrieved Memories]" and
 * returns an array of { snippet, source } objects parsed from following
 * list lines like "- ... (source: xyz)".
 */
function parseRetrievedFromSummary(
  full?: string,
): Array<{ snippet: string; source?: string; safety?: string }> {
  if (!full) return [];
  const lines = full.split(/\r?\n/);
  let start = -1;
  for (let i = 0; i < lines.length; i++) {
    if (lines[i].trim().startsWith("[Retrieved Memories]")) {
      start = i + 1;
      break;
    }
  }
  if (start === -1) return [];
  const out: Array<{ snippet: string; source?: string; safety?: string }> = [];
  for (let i = start; i < lines.length; i++) {
    const l = lines[i].trim();
    if (!l) break;
    if (!l.startsWith("-")) break;
    // strip leading '- '
    const rest = l.replace(/^-\s*/, "");
    // try to parse trailing "(source: xyz, safety: label)" or "(source: xyz)"
    const m = rest.match(
      /^(.*)\s+\(source:\s*([^,)]+)(?:,\s*safety:\s*([^)]+))?\)$/,
    );
    if (m) {
      out.push({
        snippet: m[1].trim(),
        source: m[2].trim(),
        safety: m[3]?.trim(),
      });
    } else {
      out.push({ snippet: rest });
    }
  }
  return out;
}

function RetrievedMemoriesGroup({
  compactions,
}: {
  compactions: UIContextCompaction[];
}) {
  const { client } = useOptionalClient() ?? {};
  const [recovering, setRecovering] = useState<Record<string, boolean>>({});
  const [toast, setToast] = useState<string | null>(null);

  const groups = compactions
    .map((c) => ({
      compaction: c,
      items: parseRetrievedFromSummary(c.summary_full).map((it) => ({
        ...it,
        id: (c as any).id || (c as any).compaction_id || (c as any).updated_at,
      })),
    }))
    .filter((g) => g.items.length > 0);
  if (!groups.length) return null;

  const tryAttach = (source?: string) => {
    if (!client || !source) return;
    // accept sources like "websocket:abcd-..." or plain chat ids
    const chatId = source.startsWith("websocket:")
      ? source.split(":", 2)[1]
      : source;
    if (chatId) {
      try {
        client.attach(chatId);
      } catch {
        // ignore client errors
      }
    }
  };

  const copyText = async (text: string) => {
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text);
      } else {
        const ta = document.createElement("textarea");
        ta.value = text;
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        document.body.removeChild(ta);
      }
    } catch {
      // swallow
    }
  };

  return (
    <ActivitySection
      titleKey="message.engineeringActivity.sections.retrieved"
      defaultTitle="Retrieved memories"
      icon={<FileSearch className="h-3.5 w-3.5" aria-hidden />}
    >
      {toast ? (
        <div
          role="status"
          className="fixed right-6 top-20 z-50 rounded-md border border-border/60 bg-popover px-3 py-2 text-sm font-medium text-popover-foreground shadow-lg"
        >
          {toast}
        </div>
      ) : null}
      <div className="space-y-2">
        {groups.map((g, idx) => (
          <div
            key={idx}
            className="rounded-md border border-muted/20 bg-muted/10 px-2 py-1 text-xs"
          >
            {g.items.map((it, i) => (
              <div key={i} className="mt-1 flex items-start gap-2">
                <div className="flex-1">
                  <div className="truncate font-medium">
                    {it.snippet}
                    {it.safety ? (
                      <span
                        className={cn(
                          "ml-2 inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-medium",
                          it.safety === "read-only"
                            ? "bg-emerald-100 text-emerald-800"
                            : it.safety === "requires_confirmation"
                              ? "bg-yellow-100 text-yellow-800"
                              : "bg-red-100 text-red-800",
                        )}
                      >
                        {it.safety === "read-only"
                          ? "Read-only"
                          : it.safety === "requires_confirmation"
                            ? "Needs confirm"
                            : "Unsafe"}
                      </span>
                    ) : null}
                  </div>
                  {it.source ? (
                    <div className="text-[11px] text-muted-foreground/70">
                      Source: {it.source}
                    </div>
                  ) : null}
                </div>
                <div className="flex shrink-0 items-center gap-1">
                  <AlertDialog>
                    <AlertDialogTrigger asChild>
                      <button
                        type="button"
                        className="inline-flex items-center gap-1 rounded border px-2 py-1 text-[11px] font-medium"
                        aria-label="View retrieved snippet"
                      >
                        View
                      </button>
                    </AlertDialogTrigger>
                    <AlertDialogContent>
                      <AlertDialogHeader>
                        <AlertDialogTitle>Retrieved memory</AlertDialogTitle>
                        <AlertDialogDescription>
                          Full retrieved snippet and source.
                        </AlertDialogDescription>
                      </AlertDialogHeader>
                      <div className="prose max-h-[60vh] overflow-auto mt-2">
                        <pre className="whitespace-pre-wrap">{it.snippet}</pre>
                        {it.source ? (
                          <div className="mt-2 text-xs text-muted-foreground/70">
                            Source: {it.source}
                          </div>
                        ) : null}
                      </div>
                      <AlertDialogFooter>
                        <AlertDialogCancel>Close</AlertDialogCancel>
                      </AlertDialogFooter>
                    </AlertDialogContent>
                  </AlertDialog>
                  <button
                    type="button"
                    className="inline-flex items-center gap-1 rounded border px-2 py-1 text-[11px] font-medium"
                    onClick={() => void copyText(it.snippet)}
                    aria-label="Copy snippet"
                  >
                    Copy
                  </button>
                  {it.safety && it.safety !== "unsafe" ? (
                    <button
                      type="button"
                      className="inline-flex items-center gap-2 rounded border px-2 py-1 text-[11px] font-medium"
                      onClick={async () => {
                        try {
                          setRecovering((s) => ({ ...s, [it.id]: true }));
                          const token = client ? (client as any).token : "";
                          const mode =
                            it.safety === "read-only"
                              ? "apply_readonly"
                              : "apply_with_confirmation";
                          const res = await recoverMemory(
                            token,
                            it.id,
                            mode as any,
                          );
                          setToast(
                            res?.status === "ok"
                              ? `Recovered: ${res?.id}`
                              : `Recovery: ${res?.status || "failed"}`,
                          );
                        } catch (e) {
                          setToast("Recovery failed");
                        } finally {
                          setRecovering((s) => {
                            const copy = { ...s };
                            delete copy[it.id];
                            return copy;
                          });
                          window.setTimeout(() => setToast(null), 3500);
                        }
                      }}
                      aria-label="Confirm and replay retrieved snippet"
                      disabled={!!recovering[it.id]}
                    >
                      {recovering[it.id] ? (
                        <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
                      ) : it.safety === "read-only" ? (
                        "Apply"
                      ) : (
                        "Confirm & Replay"
                      )}
                    </button>
                  ) : null}
                  {it.source ? (
                    <button
                      type="button"
                      className="inline-flex items-center gap-1 rounded border px-2 py-1 text-[11px] font-medium"
                      onClick={() => tryAttach(it.source)}
                      aria-label="Open source"
                    >
                      Open
                    </button>
                  ) : null}
                </div>
              </div>
            ))}
          </div>
        ))}
      </div>
    </ActivitySection>
  );
}

function contextCompactionReasonLabel(reason: unknown): string {
  if (reason === "token_budget") return "Token budget";
  if (reason === "replay_window") return "Replay window";
  if (reason === "idle_ttl") return "Idle memory";
  return "Compaction";
}

function ActivityPhaseIcon({ status }: { status: ActivityPhaseStatus }) {
  if (status === "failed")
    return <AlertCircle className="h-3 w-3 shrink-0" aria-hidden />;
  if (status === "done")
    return <CheckCircle2 className="h-3 w-3 shrink-0" aria-hidden />;
  if (status === "running") return <StreamingDot />;
  return (
    <span
      className="h-3 w-3 shrink-0 rounded-full border border-current/35"
      aria-hidden
    />
  );
}

function StreamingDot() {
  return (
    <span className="relative h-3 w-3 shrink-0" aria-hidden>
      <span className="absolute inset-0 rounded-full bg-current opacity-20" />
      <span className="absolute left-1/2 top-1/2 h-1.5 w-1.5 -translate-x-1/2 -translate-y-1/2 rounded-full bg-current" />
    </span>
  );
}

function TurnSnapshotPanel({
  snapshot,
  sessionKey,
  onResumeSafeTools,
}: {
  snapshot: TurnSnapshot;
  sessionKey?: string | null;
  onResumeSafeTools?: () => void;
}) {
  const { t } = useTranslation();
  return (
    <section
      aria-label="Task snapshot"
      className="rounded-md border border-border/45 bg-background/35 px-2 py-2 text-xs"
    >
      <div className="mb-1.5 flex min-w-0 items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-1.5">
          <Layers
            className="h-3.5 w-3.5 shrink-0 text-muted-foreground/70"
            aria-hidden
          />
          <span className="font-medium text-muted-foreground">
            {t("message.engineeringActivity.snapshot.title", {
              defaultValue: "Task snapshot",
            })}
          </span>
        </div>
        <SnapshotStateBadge status={snapshot.status} />
      </div>
      <dl className="grid gap-1.5 sm:grid-cols-4">
        <SnapshotMetric
          label={t("message.engineeringActivity.snapshot.phase", {
            defaultValue: "Phase",
          })}
          value={`${snapshot.phaseLabel} · ${snapshot.phaseDetail}`}
        />
        <SnapshotMetric
          label={t("message.engineeringActivity.snapshot.tools", {
            defaultValue: "Tools",
          })}
          value={String(snapshot.toolCount)}
        />
        <SnapshotMetric
          label={t("message.engineeringActivity.snapshot.files", {
            defaultValue: "Files",
          })}
          value={`${snapshot.fileCount} · +${snapshot.added} -${snapshot.deleted}`}
        />
        <SnapshotMetric
          label={t("message.engineeringActivity.snapshot.checks", {
            defaultValue: "Checks",
          })}
          value={snapshot.checkDetail}
          tone={
            snapshot.checkState === "failed"
              ? "danger"
              : snapshot.checkState === "passed"
                ? "success"
                : undefined
          }
        />
      </dl>
      <div className="mt-2 grid gap-2 lg:grid-cols-2 xl:grid-cols-4">
        <WorkbenchPanel
          title="Task plan"
          subtitle="Current, completed, and waiting steps"
        >
          <TaskPlanPanel items={snapshot.taskPlanItems} />
        </WorkbenchPanel>
        <WorkbenchPanel
          title="Check results"
          subtitle="Focused check status and latest failure"
        >
          <CheckResultsPanel summary={snapshot.checkSummary} />
        </WorkbenchPanel>
        <WorkbenchPanel
          title="Diff preview"
          subtitle="Edited files, size hints, and file-level impact"
        >
          <DiffPreviewPanel preview={snapshot.diffPreview} />
        </WorkbenchPanel>
        <WorkbenchPanel
          title="Turn summary"
          subtitle="Edits, risks, and next action"
        >
          <TurnSummaryPanel summary={snapshot.turnSummary} />
        </WorkbenchPanel>
      </div>
      {snapshot.reviewRequiredToolCount > 0 ||
      snapshot.needsInputToolCount > 0 ||
      snapshot.blockedToolCount > 0 ? (
        <div className="mt-1.5 flex items-start gap-1.5 rounded-md border border-amber-500/20 bg-amber-500/10 px-2 py-1.5 text-[11px] leading-4 text-amber-800 dark:text-amber-200">
          <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
          <span className="min-w-0">
            {t("message.engineeringActivity.snapshot.reviewBeforeRetry", {
              review: snapshot.reviewRequiredToolCount,
              needsInput: snapshot.needsInputToolCount,
              blocked: snapshot.blockedToolCount,
              defaultValue: recoveryReviewSummary(snapshot),
            })}
          </span>
        </div>
      ) : null}
      {snapshot.recoveryReviewItems.length > 0 ? (
        <RecoveryReviewList
          items={snapshot.recoveryReviewItems}
          sessionKey={sessionKey}
          onResumeSafeTools={onResumeSafeTools}
          safeResumeCount={snapshot.safeResumeToolCount}
        />
      ) : null}
      <div className="mt-1.5 flex flex-wrap items-center gap-1.5 text-[10.5px] text-muted-foreground/70">
        <span
          className={cn(
            "rounded-full border px-1.5 py-0.5",
            snapshot.hasFailures
              ? "border-destructive/20 bg-destructive/10 text-destructive"
              : "border-emerald-500/20 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300",
          )}
        >
          {snapshot.hasFailures
            ? t("message.engineeringActivity.snapshot.hasFailures", {
                defaultValue: "Needs attention",
              })
            : t("message.engineeringActivity.snapshot.noFailures", {
                defaultValue: "No failures",
              })}
        </span>
        <span className="rounded-full border border-border/45 px-1.5 py-0.5">
          {snapshot.source === "recovered"
            ? t("message.engineeringActivity.snapshot.recovered", {
                defaultValue: "Recovered checkpoint",
              })
            : snapshot.recoverable
              ? t("message.engineeringActivity.snapshot.recoverable", {
                  defaultValue: "Rebuilt from history",
                })
              : t("message.engineeringActivity.snapshot.live", {
                  defaultValue: "Live turn",
                })}
        </span>
        {snapshot.reusedToolCount > 0 ? (
          <span className="rounded-full border border-emerald-500/20 bg-emerald-500/10 px-1.5 py-0.5 text-emerald-700 dark:text-emerald-300">
            {t("message.engineeringActivity.snapshot.reusedTools", {
              count: snapshot.reusedToolCount,
              defaultValue: `Reused ${snapshot.reusedToolCount}`,
            })}
          </span>
        ) : null}
        {snapshot.compensationToolCount > 0 ? (
          <span className="rounded-full border border-amber-500/20 bg-amber-500/10 px-1.5 py-0.5 text-amber-700 dark:text-amber-300">
            {t("message.engineeringActivity.snapshot.compensatedTools", {
              count: snapshot.compensationToolCount,
              defaultValue: `Compensated ${snapshot.compensationToolCount}`,
            })}
          </span>
        ) : null}
        {snapshot.retryableToolCount > 0 ? (
          <span className="rounded-full border border-sky-500/20 bg-sky-500/10 px-1.5 py-0.5 text-sky-700 dark:text-sky-300">
            {t("message.engineeringActivity.snapshot.retryableTools", {
              count: snapshot.retryableToolCount,
              defaultValue: `Retryable ${snapshot.retryableToolCount}`,
            })}
          </span>
        ) : null}
        {snapshot.resumableToolCount > 0 ? (
          <span className="rounded-full border border-violet-500/20 bg-violet-500/10 px-1.5 py-0.5 text-violet-700 dark:text-violet-300">
            {t("message.engineeringActivity.snapshot.resumableTools", {
              count: snapshot.resumableToolCount,
              defaultValue: `Resumable ${snapshot.resumableToolCount}`,
            })}
          </span>
        ) : null}
        {snapshot.reviewRequiredToolCount > 0 ? (
          <span className="rounded-full border border-amber-500/20 bg-amber-500/10 px-1.5 py-0.5 text-amber-700 dark:text-amber-300">
            {t("message.engineeringActivity.snapshot.reviewRequiredTools", {
              count: snapshot.reviewRequiredToolCount,
              defaultValue: `Review ${snapshot.reviewRequiredToolCount}`,
            })}
          </span>
        ) : null}
        {snapshot.blockedToolCount > 0 ? (
          <span className="rounded-full border border-destructive/20 bg-destructive/10 px-1.5 py-0.5 text-destructive">
            {t("message.engineeringActivity.snapshot.blockedTools", {
              count: snapshot.blockedToolCount,
              defaultValue: `Blocked ${snapshot.blockedToolCount}`,
            })}
          </span>
        ) : null}
        {snapshot.requiresUserToolCount > 0 ? (
          <span className="rounded-full border border-amber-500/20 bg-amber-500/10 px-1.5 py-0.5 text-amber-700 dark:text-amber-300">
            {t("message.engineeringActivity.snapshot.requiresUserTools", {
              count: snapshot.requiresUserToolCount,
              defaultValue: `Needs input ${snapshot.requiresUserToolCount}`,
            })}
          </span>
        ) : null}
      </div>
    </section>
  );
}

function RecoveryReviewList({
  items,
  sessionKey,
  onResumeSafeTools,
  safeResumeCount,
}: {
  items: RecoveryReviewItem[];
  sessionKey?: string | null;
  onResumeSafeTools?: () => void;
  safeResumeCount: number;
}) {
  const { t } = useTranslation();
  const clientContext = useOptionalClient();
  const groupedItems = groupRecoveryReviewItems(items);
  const [submittingId, setSubmittingId] = useState<string | null>(null);
  const [errorById, setErrorById] = useState<Record<string, string>>({});
  const [inputById, setInputById] = useState<Record<string, string>>({});
  const [localStateById, setLocalStateById] = useState<
    Record<string, RecoveryReviewLocalState>
  >({});

  const submitAction = async (
    item: RecoveryReviewItem,
    action: "confirm_retry" | "provide_input",
  ) => {
    if (!clientContext?.token || !item.toolCallId || !sessionKey) return;
    if (action === "provide_input" && !(inputById[item.toolCallId] || "").trim()) {
      setErrorById((prev) => ({ ...prev, [item.toolCallId!]: "Input required" }));
      return;
    }
    setSubmittingId(item.toolCallId);
    setErrorById((prev) => ({ ...prev, [item.toolCallId!]: "" }));
    try {
      await applyRecoveryAction(clientContext.token, {
        sessionKey,
        toolCallId: item.toolCallId,
        action,
        userInput:
          action === "provide_input" ? inputById[item.toolCallId].trim() : undefined,
      });
      setLocalStateById((prev) => ({
        ...prev,
        [item.toolCallId!]: {
          reviewState:
            action === "confirm_retry" ? "confirmed" : "input_provided",
          statusLabel:
            action === "confirm_retry" ? "Retry confirmed" : "Input collected",
        },
      }));
      if (action === "provide_input") {
        setInputById((prev) => ({
          ...prev,
          [item.toolCallId!]: "",
        }));
      }
    } catch {
      setErrorById((prev) => ({
        ...prev,
        [item.toolCallId!]: "Could not apply action",
      }));
    } finally {
      setSubmittingId(null);
    }
  };
  return (
    <div
      id="recovery-review"
      aria-label="Recovery review"
      className="mt-1.5 rounded-md border border-border/40 bg-muted/15"
    >
      <div className="border-b border-border/35 px-2 py-1 text-[10px] font-medium uppercase tracking-[0.08em] text-muted-foreground/60">
        {t("message.engineeringActivity.snapshot.recoveryReview", {
          defaultValue: "Recovery review",
        })}
      </div>
      <div className="space-y-2 px-2 py-2">
        {groupedItems.map(({ group, items: groupItems }) => (
          <div
            key={group}
            className="rounded-md border border-border/35 bg-background/35"
          >
            <div className="flex items-center justify-between gap-2 border-b border-border/30 px-2 py-1.5">
              <div className="flex min-w-0 items-center gap-1.5">
                <RecoveryReviewBadge group={group} />
                <span className="text-[11px] font-medium text-muted-foreground/70">
                  {groupItems.length}
                </span>
              </div>
              {group === "safe_resume" && onResumeSafeTools && safeResumeCount > 0 ? (
                <button
                  type="button"
                  onClick={onResumeSafeTools}
                  className="inline-flex items-center rounded border border-emerald-500/25 bg-emerald-500/10 px-2 py-1 text-[11px] font-medium text-emerald-700 hover:bg-emerald-500/15 dark:text-emerald-300"
                >
                  Resume safe tools
                </button>
              ) : (
                <span className="text-[10px] font-medium text-muted-foreground/55">
                  {groupActionHint(group)}
                </span>
              )}
            </div>
            <div className="divide-y divide-border/25">
              {groupItems.map((item, index) => (
                <RecoveryReviewRow
                  key={`${item.toolCallId || item.name}-${index}`}
                  item={item}
                  sessionKey={sessionKey}
                  token={clientContext?.token}
                  inputValue={item.toolCallId ? (inputById[item.toolCallId] ?? "") : ""}
                  error={item.toolCallId ? errorById[item.toolCallId] : undefined}
                  isSubmitting={submittingId === item.toolCallId}
                  localState={item.toolCallId ? localStateById[item.toolCallId] : undefined}
                  onInputChange={(value) => {
                    if (!item.toolCallId) return;
                    setInputById((prev) => ({
                      ...prev,
                      [item.toolCallId!]: value,
                    }));
                  }}
                  onSubmitAction={submitAction}
                />
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function RecoveryReviewRow({
  item,
  sessionKey,
  token,
  inputValue,
  error,
  isSubmitting,
  localState,
  onInputChange,
  onSubmitAction,
}: {
  item: RecoveryReviewItem;
  sessionKey?: string | null;
  token?: string;
  inputValue: string;
  error?: string;
  isSubmitting: boolean;
  localState?: RecoveryReviewLocalState;
  onInputChange: (value: string) => void;
  onSubmitAction: (
    item: RecoveryReviewItem,
    action: "confirm_retry" | "provide_input",
  ) => Promise<void>;
}) {
  const effectiveState = localState?.reviewState || item.reviewState || "pending";
  const statusLabel =
    localState?.statusLabel ||
    item.statusLabel ||
    item.actionLabel ||
    (item.recoveryAction ? formatRecoveryToken(item.recoveryAction) : "");
  const showConfirmButton =
    item.group === "review_required"
    && item.toolCallId
    && !["confirmed", "input_provided"].includes(effectiveState);
  const showInputForm =
    item.group === "needs_input"
    && item.toolCallId
    && effectiveState !== "input_provided";

  return (
    <div className="grid gap-2 px-2 py-1.5 sm:grid-cols-[minmax(0,1fr)_auto]">
      <div className="min-w-0">
        <div className="flex min-w-0 flex-wrap items-center gap-1.5">
          <span className="truncate font-medium text-muted-foreground">
            {item.name}
          </span>
          {item.reviewKind ? (
            <span className="rounded-full border border-border/40 px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground/70">
              {formatRecoveryToken(item.reviewKind)}
            </span>
          ) : null}
          {item.scope ? (
            <span className="rounded-full border border-border/40 px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground/60">
              {item.scope}
            </span>
          ) : null}
          {statusLabel ? (
            <span className="rounded-full border border-border/40 bg-background/40 px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground/70">
              {statusLabel}
            </span>
          ) : null}
        </div>
        {item.summary ? (
          <div className="mt-0.5 truncate text-[11px] text-muted-foreground/75">
            {item.summary}
          </div>
        ) : null}
        {item.reason ? (
          <div className="mt-0.5 truncate text-[11px] text-muted-foreground/65">
            {formatRecoveryToken(item.reason)}
          </div>
        ) : null}
        {showInputForm ? (
          <div className="mt-2 flex flex-col gap-1.5">
            <input
              value={inputValue}
              onChange={(event) => onInputChange(event.target.value)}
              placeholder={item.inputPlaceholder || "Provide missing input"}
              className="h-8 rounded border border-border/50 bg-background px-2 text-[11px]"
            />
            {error ? (
              <span className="text-[10px] text-destructive">{error}</span>
            ) : null}
          </div>
        ) : null}
      </div>
      <div className="flex items-center justify-end gap-2 self-center">
        {showConfirmButton ? (
          <button
            type="button"
            disabled={isSubmitting || !token || !sessionKey}
            onClick={() => void onSubmitAction(item, "confirm_retry")}
            className="inline-flex items-center rounded border border-amber-500/25 bg-amber-500/10 px-2 py-1 text-[11px] font-medium text-amber-700 disabled:opacity-60 dark:text-amber-300"
          >
            Confirm retry
          </button>
        ) : null}
        {showInputForm ? (
          <button
            type="button"
            disabled={isSubmitting || !token || !sessionKey}
            onClick={() => void onSubmitAction(item, "provide_input")}
            className="inline-flex items-center rounded border border-sky-500/25 bg-sky-500/10 px-2 py-1 text-[11px] font-medium text-sky-700 disabled:opacity-60 dark:text-sky-300"
          >
            Submit input
          </button>
        ) : null}
      </div>
    </div>
  );
}

function groupRecoveryReviewItems(items: RecoveryReviewItem[]): Array<{
  group: RecoveryReviewGroup;
  items: RecoveryReviewItem[];
}> {
  const priority: RecoveryReviewGroup[] = [
    "safe_resume",
    "review_required",
    "needs_input",
    "blocked",
  ];
  const grouped = new Map<RecoveryReviewGroup, RecoveryReviewItem[]>();
  for (const item of items) {
    const current = grouped.get(item.group) ?? [];
    current.push(item);
    grouped.set(item.group, current);
  }
  const ordered: Array<{ group: RecoveryReviewGroup; items: RecoveryReviewItem[] }> = [];
  for (const group of priority) {
    const groupItems = grouped.get(group);
    if (groupItems?.length) {
      ordered.push({ group, items: groupItems });
      grouped.delete(group);
    }
  }
  for (const [group, groupItems] of grouped.entries()) {
    ordered.push({ group, items: groupItems });
  }
  return ordered;
}

function groupActionHint(group: RecoveryReviewGroup): string {
  if (group === "review_required") return "Review details before retry";
  if (group === "needs_input") return "Collect missing input";
  if (group === "blocked") return "Revise the request first";
  return "Pending action";
}

function jumpToWorkbenchAnchor(anchorId: string) {
  if (typeof document === "undefined") return;
  document.getElementById(anchorId)?.scrollIntoView({
    block: "nearest",
    behavior: "smooth",
  });
}

function RecoveryReviewBadge({ group }: { group: RecoveryReviewGroup }) {
  const label =
    group === "safe_resume"
      ? "Safe resume"
      : group === "review_required"
        ? "Review"
        : group === "needs_input"
          ? "Needs input"
          : group === "blocked"
            ? "Blocked"
            : formatRecoveryToken(group);
  return (
    <span
      className={cn(
        "inline-flex shrink-0 rounded-full border px-1.5 py-0.5 text-[10px] font-medium leading-none",
        group === "safe_resume" &&
          "border-emerald-500/20 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300",
        group === "review_required" &&
          "border-amber-500/20 bg-amber-500/10 text-amber-700 dark:text-amber-300",
        group === "needs_input" &&
          "border-sky-500/20 bg-sky-500/10 text-sky-700 dark:text-sky-300",
        group === "blocked" &&
          "border-destructive/20 bg-destructive/10 text-destructive",
        !["safe_resume", "review_required", "needs_input", "blocked"].includes(
          group,
        ) && "border-border/45 bg-background/40 text-muted-foreground/70",
      )}
    >
      {label}
    </span>
  );
}

function formatRecoveryToken(value: string): string {
  return value
    .replace(/[_-]+/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/^\w/, (first) => first.toUpperCase());
}

function recoveryReviewSummary(snapshot: TurnSnapshot): string {
  const parts: string[] = [];
  if (snapshot.reviewRequiredToolCount > 0) {
    parts.push(`${snapshot.reviewRequiredToolCount} need review`);
  }
  if (snapshot.needsInputToolCount > 0) {
    parts.push(`${snapshot.needsInputToolCount} need input`);
  }
  if (snapshot.blockedToolCount > 0) {
    parts.push(`${snapshot.blockedToolCount} blocked`);
  }
  return `Review before retry: ${parts.join(", ")}.`;
}

function WorkbenchPanel({
  title,
  subtitle,
  children,
}: {
  title: string;
  subtitle: string;
  children: ReactNode;
}) {
  return (
    <section className="rounded border border-border/35 bg-muted/15 px-2 py-2">
      <div className="mb-1.5">
        <div className="font-medium text-muted-foreground">{title}</div>
        <div className="text-[10.5px] text-muted-foreground/65">{subtitle}</div>
      </div>
      {children}
    </section>
  );
}

function TaskPlanPanel({ items }: { items: TaskPlanItem[] }) {
  return (
    <ol className="space-y-1.5" aria-label="Task plan">
      {items.map((item) => (
        <li
          key={item.key}
          className="flex items-start gap-2 rounded border border-border/25 bg-background/35 px-2 py-1.5"
        >
          <SnapshotStateBadge status={item.status} />
          <div className="min-w-0">
            <div className="font-medium text-muted-foreground">{item.label}</div>
            <div className="text-[10.5px] text-muted-foreground/70">
              {item.detail || "No detail provided."}
            </div>
            {item.anchorId && item.actionLabel ? (
              <button
                type="button"
                onClick={() => jumpToWorkbenchAnchor(item.anchorId!)}
                className="mt-1 inline-flex items-center rounded border border-border/40 bg-background/50 px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground/75 hover:bg-background"
              >
                {item.actionLabel}
              </button>
            ) : null}
          </div>
        </li>
      ))}
    </ol>
  );
}

function CheckResultsPanel({ summary }: { summary: WorkbenchCheckSummary }) {
  const stateLabel =
    summary.failed > 0
      ? `${summary.failed} failed`
      : summary.running > 0
        ? `${summary.running} running`
        : summary.passed > 0
          ? `${summary.passed} passed`
          : "Not run";
  return (
    <div id="check-results" className="space-y-1.5" aria-label="Check results">
      <div className="grid gap-1.5 sm:grid-cols-2">
        <SnapshotMetric label="Status" value={stateLabel} tone={summary.failed > 0 ? "danger" : summary.passed > 0 ? "success" : undefined} />
        <SnapshotMetric
          label="Command"
          value={summary.primaryCommand || "No check command"}
        />
      </div>
      {summary.failureCategory || summary.relatedTarget ? (
        <div className="grid gap-1.5 sm:grid-cols-2">
          {summary.failureCategory ? (
            <SnapshotMetric
              label="Failure kind"
              value={formatRecoveryToken(summary.failureCategory)}
              tone={summary.failed > 0 ? "danger" : undefined}
            />
          ) : null}
          {summary.relatedTarget ? (
            <SnapshotMetric
              label="Related target"
              value={summary.relatedTarget}
            />
          ) : null}
        </div>
      ) : null}
      {summary.failureSummary ? (
        <div className="rounded border border-destructive/20 bg-destructive/5 px-2 py-1.5 text-[10.5px] text-destructive">
          {summary.failureSummary}
        </div>
      ) : (
        <div className="rounded border border-border/25 bg-background/35 px-2 py-1.5 text-[10.5px] text-muted-foreground/70">
          No failure summary available.
        </div>
      )}
      {summary.diagnosticLabel || summary.diagnosticHint || summary.recommendedAction ? (
        <div className="space-y-1 rounded border border-amber-500/20 bg-amber-500/5 px-2 py-1.5">
          {summary.diagnosticLabel ? (
            <div className="font-medium text-amber-800 dark:text-amber-200">
              {summary.diagnosticLabel}
            </div>
          ) : null}
          {summary.diagnosticHint ? (
            <div className="text-[10.5px] text-amber-900/80 dark:text-amber-100/85">
              {summary.diagnosticHint}
            </div>
          ) : null}
          {summary.recommendedAction ? (
            <div className="text-[10.5px] text-amber-900/80 dark:text-amber-100/85">
              Next: {summary.recommendedAction}
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function DiffPreviewPanel({ preview }: { preview: WorkbenchDiffPreview }) {
  if (preview.total === 0) {
    return (
      <div
        id="diff-preview"
        aria-label="Diff preview"
        className="rounded border border-border/25 bg-background/35 px-2 py-1.5 text-[10.5px] text-muted-foreground/70"
      >
        No file edits captured in this turn.
      </div>
    );
  }
  return (
    <div id="diff-preview" className="space-y-1.5" aria-label="Diff preview">
      <div className="grid gap-1.5 sm:grid-cols-2">
        <SnapshotMetric label="Files" value={`${preview.total} changed`} />
        <SnapshotMetric label="Flags" value={diffPreviewFlagsLabel(preview)} />
      </div>
      <ul className="space-y-1">
        {preview.items.map((edit) => (
          <li
            key={edit.key}
            className="rounded border border-border/25 bg-background/35 px-2 py-1.5"
          >
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0">
                <div className="flex min-w-0 flex-wrap items-center gap-1.5">
                  <FileChangeBadge kind={edit.changeKind} />
                  <span className="truncate font-medium text-muted-foreground">
                    {edit.path || "Pending file edit"}
                  </span>
                  <span className="rounded-full border border-border/40 px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground/70">
                    {fileAreaLabel(edit.area)}
                  </span>
                  {edit.binary ? (
                    <DiffPreviewFlag label="Binary" tone="neutral" />
                  ) : null}
                  {edit.approximate ? (
                    <DiffPreviewFlag label="Estimated" tone="neutral" />
                  ) : null}
                  {edit.status === "error" ? (
                    <DiffPreviewFlag label="Failed" tone="danger" />
                  ) : null}
                  {edit.added + edit.deleted >= LARGE_DIFF_LINE_THRESHOLD ? (
                    <DiffPreviewFlag label="Large change" tone="warning" />
                  ) : null}
                </div>
              </div>
              {!edit.binary && edit.status !== "error" ? (
                <DiffPair added={edit.added} deleted={edit.deleted} />
              ) : null}
            </div>
          </li>
        ))}
      </ul>
      {preview.total > preview.items.length ? (
        <div className="text-[10.5px] text-muted-foreground/65">
          Showing {preview.items.length} of {preview.total} changed files.
        </div>
      ) : null}
    </div>
  );
}

function TurnSummaryPanel({ summary }: { summary: WorkbenchTurnSummary }) {
  return (
    <dl className="space-y-1.5" aria-label="Turn summary">
      <SummaryRow label="Changed">{summary.modifiedSummary}</SummaryRow>
      <SummaryRow label="Checks">{summary.checksSummary}</SummaryRow>
      <SummaryRow label="Risk">{summary.riskSummary}</SummaryRow>
      <SummaryRow
        label="Next step"
        action={
          summary.nextStepAnchorId && summary.nextStepActionLabel ? (
            <button
              type="button"
              onClick={() => jumpToWorkbenchAnchor(summary.nextStepAnchorId!)}
              className="inline-flex items-center rounded border border-border/40 bg-background/50 px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground/75 hover:bg-background"
            >
              {summary.nextStepActionLabel}
            </button>
          ) : undefined
        }
      >
        {summary.nextStep}
      </SummaryRow>
    </dl>
  );
}

function SummaryRow({
  label,
  action,
  children,
}: {
  label: string;
  action?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="rounded border border-border/25 bg-background/35 px-2 py-1.5">
      <div className="flex items-center justify-between gap-2">
        <dt className="text-[10px] font-medium uppercase tracking-[0.08em] text-muted-foreground/55">
          {label}
        </dt>
        {action}
      </div>
      <dd className="mt-0.5 text-[10.5px] text-muted-foreground">{children}</dd>
    </div>
  );
}

function DiffPreviewFlag({
  label,
  tone,
}: {
  label: string;
  tone: "neutral" | "warning" | "danger";
}) {
  return (
    <span
      className={cn(
        "rounded-full border px-1.5 py-0.5 text-[10px] font-medium",
        tone === "neutral" &&
          "border-border/40 bg-background/40 text-muted-foreground/70",
        tone === "warning" &&
          "border-amber-500/20 bg-amber-500/10 text-amber-700 dark:text-amber-300",
        tone === "danger" &&
          "border-destructive/20 bg-destructive/10 text-destructive",
      )}
    >
      {label}
    </span>
  );
}

function diffPreviewFlagsLabel(preview: WorkbenchDiffPreview): string {
  const parts: string[] = [];
  if (preview.binaryCount > 0) parts.push(`${preview.binaryCount} binary`);
  if (preview.largeChangeCount > 0) {
    parts.push(`${preview.largeChangeCount} large`);
  }
  if (preview.approximateCount > 0) {
    parts.push(`${preview.approximateCount} estimated`);
  }
  if (preview.failedCount > 0) parts.push(`${preview.failedCount} failed`);
  return parts.length > 0 ? parts.join(" · ") : "No special flags";
}

function SnapshotMetric({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: "success" | "danger";
}) {
  return (
    <div className="min-w-0 rounded border border-border/35 bg-muted/20 px-2 py-1.5">
      <dt className="truncate text-[10px] font-medium uppercase tracking-[0.08em] text-muted-foreground/55">
        {label}
      </dt>
      <dd
        className={cn(
          "mt-0.5 truncate font-medium text-muted-foreground",
          tone === "success" && "text-emerald-700 dark:text-emerald-300",
          tone === "danger" && "text-destructive",
        )}
      >
        {value}
      </dd>
    </div>
  );
}

function activeSkillSourceLabel(source?: string): string {
  if (source === "always") return "always";
  if (source === "explicit") return "explicit";
  if (source === "auto") return "auto";
  return source || "skill";
}

function activeSkillTitle(skill: NonNullable<UIActiveSkills["skills"]>[number]): string {
  const parts = [
    skill.reason,
    ...(skill.matched_keywords?.length
      ? [`keywords: ${skill.matched_keywords.join(", ")}`]
      : []),
  ].filter(Boolean);
  return parts.join(" | ");
}

function SnapshotStateBadge({ status }: { status: ActivityPhaseStatus }) {
  const { t } = useTranslation();
  const label = t(`message.engineeringActivity.snapshot.status.${status}`, {
    defaultValue:
      status === "running"
        ? "Running"
        : status === "failed"
          ? "Failed"
          : status === "done"
            ? "Done"
            : "Pending",
  });
  return (
    <span
      className={cn(
        "inline-flex shrink-0 rounded-full border px-1.5 py-0.5 text-[10px] font-medium leading-none",
        status === "pending" &&
          "border-border/45 bg-muted/25 text-muted-foreground/65",
        status === "running" &&
          "border-sky-500/20 bg-sky-500/10 text-sky-700 dark:text-sky-300",
        status === "done" &&
          "border-emerald-500/20 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300",
        status === "failed" &&
          "border-destructive/20 bg-destructive/10 text-destructive",
      )}
    >
      {label}
    </span>
  );
}

function EngineeringTraceSections({
  traces,
  isTurnStreaming,
}: {
  traces: ParsedToolTrace[];
  isTurnStreaming: boolean;
}) {
  const checkTraces = traces.filter((trace) => trace.category === "check");
  const toolTraces = traces.filter((trace) => trace.category !== "check");
  const scheduling = summarizeToolScheduling(traces);
  return (
    <>
      {toolTraces.length ? (
        <ActivitySection
          titleKey="message.engineeringActivity.sections.tools"
          defaultTitle="Tool steps"
          icon={<Wrench className="h-3.5 w-3.5" aria-hidden />}
        >
          {scheduling ? <ToolSchedulingPanel summary={scheduling} /> : null}
          <TraceList traces={toolTraces} isTurnStreaming={isTurnStreaming} />
        </ActivitySection>
      ) : null}
      {checkTraces.length ? (
        <ActivitySection
          titleKey="message.engineeringActivity.sections.checks"
          defaultTitle="Checks"
          icon={<CheckCircle2 className="h-3.5 w-3.5" aria-hidden />}
        >
          <TraceList traces={checkTraces} isTurnStreaming={isTurnStreaming} />
        </ActivitySection>
      ) : null}
    </>
  );
}

function ActivitySection({
  titleKey,
  defaultTitle,
  icon,
  children,
}: {
  titleKey: string;
  defaultTitle: string;
  icon: ReactNode;
  children: ReactNode;
}) {
  const { t } = useTranslation();
  return (
    <section className="border-l border-muted-foreground/15 pl-3">
      <div className="mb-1 flex items-center gap-1.5 text-[11px] font-medium uppercase tracking-[0.08em] text-muted-foreground/70">
        {icon}
        <span>{t(titleKey, { defaultValue: defaultTitle })}</span>
      </div>
      {children}
    </section>
  );
}

function summarizeToolScheduling(
  traces: ParsedToolTrace[],
): ToolSchedulingSummary | null {
  const scheduled = traces.filter(
    (trace) =>
      trace.batchId ||
      trace.concurrencyLimit !== undefined ||
      trace.status === "queued" ||
      trace.status === "running",
  );
  if (!scheduled.length) return null;
  const concurrencyLimits = scheduled
    .map((trace) => trace.concurrencyLimit)
    .filter(
      (value): value is number =>
        typeof value === "number" && Number.isFinite(value),
    );
  const batchIndexes = scheduled
    .map((trace) => trace.batchIndex)
    .filter(
      (value): value is number =>
        typeof value === "number" && Number.isFinite(value),
    );
  const batchCounts = scheduled
    .map((trace) => trace.batchCount)
    .filter(
      (value): value is number =>
        typeof value === "number" && Number.isFinite(value),
    );
  return {
    queued: scheduled.filter((trace) => trace.status === "queued").length,
    running: scheduled.filter((trace) => trace.status === "running").length,
    completed: scheduled.filter((trace) => trace.status === "passed").length,
    failed: scheduled.filter((trace) => trace.status === "failed").length,
    total: scheduled.length,
    concurrencyLimit: concurrencyLimits.length
      ? Math.max(...concurrencyLimits)
      : undefined,
    batchCount: batchCounts.length
      ? Math.max(...batchCounts)
      : batchIndexes.length
        ? Math.max(...batchIndexes)
        : undefined,
  };
}

function ToolSchedulingPanel({ summary }: { summary: ToolSchedulingSummary }) {
  return (
    <div
      data-testid="tool-scheduling-summary"
      className="mb-1.5 grid gap-1.5 rounded-md border border-sky-500/20 bg-sky-500/10 px-2 py-2 text-xs text-sky-800 dark:text-sky-200 sm:grid-cols-4"
    >
      <SnapshotMetric label="Running" value={String(summary.running)} />
      <SnapshotMetric label="Queued" value={String(summary.queued)} />
      <SnapshotMetric
        label="Limit"
        value={
          summary.concurrencyLimit ? String(summary.concurrencyLimit) : "Auto"
        }
      />
      <SnapshotMetric
        label="Batches"
        value={summary.batchCount ? String(summary.batchCount) : "1"}
        tone={summary.failed > 0 ? "danger" : undefined}
      />
    </div>
  );
}

function TraceList({
  traces,
  isTurnStreaming,
}: {
  traces: ParsedToolTrace[];
  isTurnStreaming: boolean;
}) {
  return (
    <ul className="space-y-1">
      {traces.map((trace, index) => (
        <TraceRow
          key={`${trace.raw}-${index}`}
          trace={trace}
          isTurnStreaming={isTurnStreaming}
        />
      ))}
    </ul>
  );
}

function TraceRow({
  trace,
  isTurnStreaming,
}: {
  trace: ParsedToolTrace;
  isTurnStreaming: boolean;
}) {
  const { t } = useTranslation();
  const Icon = traceIcon(trace.category);
  const action = t(`message.engineeringActivity.actions.${trace.category}`, {
    defaultValue: trace.category,
  });
  const detail = trace.command ?? trace.target ?? compactToolTraceLabel(trace);
  const status = effectiveTraceStatus(trace, isTurnStreaming);
  return (
    <li className="grid grid-cols-[auto_minmax(0,1fr)] gap-2 rounded-md px-2 py-1.5 text-xs">
      <Icon
        className="mt-0.5 h-3.5 w-3.5 text-muted-foreground/65"
        aria-hidden
      />
      <div className="min-w-0">
        <div className="flex min-w-0 items-center gap-1.5">
          <span className="shrink-0 font-medium text-muted-foreground">
            {action}
          </span>
          <StatusBadge status={status} />
          <RiskBadge trace={trace} />
          <CapabilityBadge trace={trace} />
          <RecoveryBadge trace={trace} />
          <SchedulingBadge trace={trace} />
          <span className="truncate text-muted-foreground/85">{detail}</span>
        </div>
        {trace.summary ? (
          <div className="mt-0.5 truncate text-[11px] text-muted-foreground/75">
            {trace.summary}
          </div>
        ) : null}
        {trace.diagnosticHint || trace.recommendedAction ? (
          <div className="mt-0.5 truncate text-[10.5px] text-amber-700/90 dark:text-amber-200/85">
            {trace.diagnosticHint}
            {trace.diagnosticHint && trace.recommendedAction ? " " : ""}
            {trace.recommendedAction}
          </div>
        ) : null}
        {trace.elapsedMs !== undefined || trace.durationMs !== undefined ? (
          <div className="mt-0.5 truncate text-[10.5px] text-muted-foreground/60">
            {traceTimingLabel(trace)}
          </div>
        ) : null}
        <div className="mt-0.5 truncate font-mono text-[10.5px] text-muted-foreground/55">
          {trace.raw}
        </div>
      </div>
    </li>
  );
}

function SchedulingBadge({ trace }: { trace: ParsedToolTrace }) {
  if (!trace.batchIndex && !trace.queuePosition && !trace.concurrencyLimit)
    return null;
  const parts: string[] = [];
  if (trace.batchIndex) {
    parts.push(
      trace.batchCount
        ? `B${trace.batchIndex}/${trace.batchCount}`
        : `B${trace.batchIndex}`,
    );
  }
  if (trace.queuePosition) parts.push(`#${trace.queuePosition}`);
  if (trace.concurrencyLimit) parts.push(`limit ${trace.concurrencyLimit}`);
  return (
    <span className="inline-flex shrink-0 rounded-full border border-sky-500/20 bg-sky-500/10 px-1.5 py-0.5 text-[10px] font-medium leading-none text-sky-700 dark:text-sky-300">
      {parts.join(" · ")}
    </span>
  );
}

function traceTimingLabel(trace: ParsedToolTrace): string {
  if (trace.durationMs !== undefined)
    return `Duration ${formatMs(trace.durationMs)}`;
  if (trace.elapsedMs !== undefined)
    return `Elapsed ${formatMs(trace.elapsedMs)}`;
  return "";
}

function formatMs(value: number): string {
  if (value >= 1000)
    return `${(value / 1000).toFixed(value >= 10_000 ? 0 : 1)}s`;
  return `${Math.max(0, Math.round(value))}ms`;
}

function RiskBadge({ trace }: { trace: ParsedToolTrace }) {
  const label = trace.blocked
    ? "Blocked"
    : trace.riskLevel === "high"
      ? "High risk"
      : trace.riskCategory
        ? riskCategoryLabel(trace.riskCategory)
        : null;
  if (!label) return null;
  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center rounded-full border px-1.5 py-0.5 text-[10px] font-medium leading-none",
        trace.blocked &&
          "border-destructive/25 bg-destructive/10 text-destructive",
        !trace.blocked &&
          trace.riskLevel === "high" &&
          "border-amber-500/25 bg-amber-500/10 text-amber-700 dark:text-amber-200",
        !trace.blocked &&
          trace.riskLevel !== "high" &&
          "border-border/45 bg-muted/25 text-muted-foreground/70",
      )}
    >
      {label}
    </span>
  );
}

function CapabilityBadge({ trace }: { trace: ParsedToolTrace }) {
  const labels = capabilityLabels(trace);
  if (!labels.length) return null;
  return (
    <>
      {labels.map((label) => (
        <span
          key={label}
          className="inline-flex shrink-0 items-center rounded-full border border-violet-500/20 bg-violet-500/10 px-1.5 py-0.5 text-[10px] font-medium leading-none text-violet-700 dark:text-violet-200"
        >
          {label}
        </span>
      ))}
    </>
  );
}

function capabilityLabels(trace: ParsedToolTrace): string[] {
  const labels: string[] = [];
  if (trace.exclusive) labels.push("Exclusive");
  if (trace.concurrencySafe) labels.push("Parallel safe");
  if (trace.readOnly) labels.push("Read only");
  if (trace.configKey) labels.push(`Config: ${trace.configKey}`);
  return labels;
}

function RecoveryBadge({ trace }: { trace: ParsedToolTrace }) {
  const label = recoveryLabel(trace);
  if (!label) return null;
  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center rounded-full border px-1.5 py-0.5 text-[10px] font-medium leading-none",
        trace.needsUserInput &&
          "border-amber-500/25 bg-amber-500/10 text-amber-700 dark:text-amber-200",
        !trace.needsUserInput &&
          trace.retryable &&
          "border-sky-500/20 bg-sky-500/10 text-sky-700 dark:text-sky-300",
        !trace.needsUserInput &&
          !trace.retryable &&
          "border-border/45 bg-muted/25 text-muted-foreground/70",
      )}
    >
      {label}
    </span>
  );
}

function recoveryLabel(trace: ParsedToolTrace): string | null {
  if (trace.diagnosticLabel) return trace.diagnosticLabel;
  if (trace.needsUserInput) return "Needs input";
  if (trace.blocked) return "Blocked";
  if (trace.retryable) return "Retryable";
  if (trace.failureCategory === "external_lookup_repeated")
    return "Use context";
  if (trace.failureCategory === "workspace_boundary") return "Boundary";
  return null;
}

function riskCategoryLabel(category: string): string {
  if (category === "read") return "Read";
  if (category === "write") return "Write";
  if (category === "shell") return "Shell";
  if (category === "network") return "Network";
  if (category === "mcp") return "MCP";
  return "Tool";
}

function effectiveTraceStatus(
  trace: ParsedToolTrace,
  isTurnStreaming: boolean,
): ToolTraceStatus {
  if (trace.status !== "unknown") return trace.status;
  if (trace.category === "check" && isTurnStreaming) return "running";
  return "unknown";
}

function StatusBadge({ status }: { status: ToolTraceStatus }) {
  const { t } = useTranslation();
  if (status === "unknown") return null;
  const label = t(`message.engineeringActivity.status.${status}`, {
    defaultValue:
      status === "running"
        ? "Running"
        : status === "queued"
          ? "Queued"
          : status === "passed"
            ? "Passed"
            : "Failed",
  });
  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center rounded-full border px-1.5 py-0.5 text-[10px] font-medium leading-none",
        status === "queued" &&
          "border-border/45 bg-muted/25 text-muted-foreground/70",
        status === "running" &&
          "border-sky-500/20 bg-sky-500/10 text-sky-700 dark:text-sky-300",
        status === "passed" &&
          "border-emerald-500/20 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300",
        status === "failed" &&
          "border-destructive/20 bg-destructive/10 text-destructive",
      )}
    >
      {label}
    </span>
  );
}

function traceIcon(category: ToolTraceCategory) {
  switch (category) {
    case "read":
      return FileSearch;
    case "edit":
      return PencilLine;
    case "check":
    case "shell":
      return TerminalSquare;
    case "search":
      return Search;
    default:
      return Wrench;
  }
}

function FileEditRow({ edit }: { edit: FileEditSummary }) {
  const { t } = useTranslation();
  const editing = edit.status === "editing";
  const failed = edit.status === "error";
  const hasCountedDiff = !failed && !edit.binary;
  return (
    <li className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-3 rounded-md px-2 py-1.5 text-xs">
      <div className="flex min-w-0 items-center gap-2">
        {!edit.pending && edit.path ? (
          <FileChangeBadge kind={edit.changeKind} />
        ) : null}
        {edit.pending && !edit.path ? (
          <StreamingLabelSheen
            active={editing}
            className="min-w-0 text-[12px] font-medium text-muted-foreground"
          >
            {t("message.fileEditPreparing", {
              defaultValue: "Preparing file edit…",
            })}
          </StreamingLabelSheen>
        ) : (
          <FileReferenceChip
            path={edit.path}
            tooltipPath={edit.absolute_path}
            display="path"
            active={editing}
            className="min-w-0"
            textClassName="text-[12px]"
            testId="activity-file-reference"
          />
        )}
        {edit.extension ? (
          <span className="shrink-0 rounded border border-border/45 px-1 py-0.5 font-mono text-[10px] leading-none text-muted-foreground/60">
            {edit.extension}
          </span>
        ) : null}
        {failed ? (
          <span className="inline-flex shrink-0 items-center gap-1 text-[10.5px] font-medium text-destructive/75">
            <AlertCircle className="h-3 w-3" aria-hidden />
            {t("message.fileEditFailed", { defaultValue: "Failed" })}
          </span>
        ) : null}
        {edit.approximate && !failed ? (
          <span className="shrink-0 text-[10.5px] font-medium text-muted-foreground/55">
            {t("message.fileEditApproximate", { defaultValue: "estimated" })}
          </span>
        ) : null}
      </div>
      {hasCountedDiff ? (
        <DiffPair added={edit.added} deleted={edit.deleted} />
      ) : null}
    </li>
  );
}

function FileChangeBadge({ kind }: { kind: FileChangeKind }) {
  const { t } = useTranslation();
  const label = t(`message.fileChange.${kind}`, {
    defaultValue:
      kind === "added" ? "Added" : kind === "deleted" ? "Deleted" : "Modified",
  });
  return (
    <span
      data-testid="activity-file-change-kind"
      className={cn(
        "inline-flex shrink-0 rounded-full border px-1.5 py-0.5 text-[10px] font-medium leading-none",
        kind === "added" &&
          "border-emerald-500/20 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300",
        kind === "modified" &&
          "border-sky-500/20 bg-sky-500/10 text-sky-700 dark:text-sky-300",
        kind === "deleted" &&
          "border-rose-500/20 bg-rose-500/10 text-rose-700 dark:text-rose-300",
      )}
    >
      {label}
    </span>
  );
}

function inferFileChangeKind(added: number, deleted: number): FileChangeKind {
  if (added > 0 && deleted === 0) return "added";
  if (deleted > 0 && added === 0) return "deleted";
  return "modified";
}

function fileExtension(path: string): string {
  const name = shortFileName(path);
  const index = name.lastIndexOf(".");
  if (index <= 0 || index === name.length - 1) return "";
  return name.slice(index + 1).toLowerCase();
}

function fileArea(path: string): FileArea {
  const normalized = path.replace(/\\/g, "/").toLowerCase();
  const name = shortFileName(normalized);
  if (
    normalized.includes("/test/") ||
    normalized.includes("/tests/") ||
    normalized.includes("__tests__") ||
    /\.(test|spec)\.[cm]?[jt]sx?$/.test(name) ||
    /^test_.*\.py$/.test(name)
  ) {
    return "test";
  }
  if (/\.(md|mdx|rst|txt)$/.test(name) || normalized.includes("/docs/"))
    return "docs";
  if (
    /(^|\/)(package\.json|bun\.lockb?|pnpm-lock\.yaml|yarn\.lock|vite\.config\.ts|tsconfig.*\.json|pyproject\.toml|ruff\.toml|pytest\.ini|dockerfile|docker-compose.*\.ya?ml|\.env.*)$/.test(
      normalized,
    ) ||
    /\.(json|ya?ml|toml|ini)$/.test(name)
  ) {
    return "config";
  }
  if (
    /\.(tsx?|jsx?|css|scss|sass|html|vue|svelte)$/.test(name) ||
    normalized.startsWith("webui/")
  ) {
    return "frontend";
  }
  if (
    /\.(py|rs|go|java|kt|c|cc|cpp|h|hpp|cs)$/.test(name) ||
    normalized.startsWith("nanobot/")
  ) {
    return "backend";
  }
  return "other";
}

function fileAreaLabel(area: FileArea): string {
  switch (area) {
    case "frontend":
      return "Frontend";
    case "backend":
      return "Backend";
    case "test":
      return "Tests";
    case "docs":
      return "Docs";
    case "config":
      return "Config";
    default:
      return "Other";
  }
}

function groupedFileEdits(
  edits: FileEditSummary[],
): Array<{ area: FileArea; edits: FileEditSummary[] }> {
  const order: FileArea[] = [
    "frontend",
    "backend",
    "test",
    "docs",
    "config",
    "other",
  ];
  return order
    .map((area) => ({
      area,
      edits: edits.filter((edit) => edit.area === area),
    }))
    .filter((group) => group.edits.length > 0);
}

function fileChangeSummaryLabel(
  edits: FileEditSummary[],
  editing: boolean,
  failed: boolean,
): string {
  if (failed) return `Failed {{count}} files`;
  if (editing) return `Editing {{count}} files`;
  const added = edits.filter((edit) => edit.changeKind === "added").length;
  const deleted = edits.filter((edit) => edit.changeKind === "deleted").length;
  const modified = edits.filter(
    (edit) => edit.changeKind === "modified",
  ).length;
  const parts: string[] = [];
  if (modified) parts.push(`modified ${modified}`);
  if (added) parts.push(`added ${added}`);
  if (deleted) parts.push(`deleted ${deleted}`);
  return parts.length ? `Files ${parts.join(", ")}` : `Edited {{count}} files`;
}

function DiffPair({ added, deleted }: { added: number; deleted: number }) {
  return (
    <span className="inline-flex shrink-0 translate-y-[0.055em] items-center gap-1.5 tabular-nums">
      <DiffValue
        sign="+"
        value={added}
        className="text-emerald-600/75 dark:text-emerald-300/75"
      />
      <DiffValue
        sign="-"
        value={deleted}
        className="text-rose-600/70 dark:text-rose-300/75"
      />
    </span>
  );
}

function DiffValue({
  sign,
  value,
  className,
}: {
  sign: string;
  value: number;
  className: string;
}) {
  const safeValue = Number.isFinite(value) ? Math.max(0, Math.round(value)) : 0;
  return (
    <span
      className={cn("inline-flex", className)}
      aria-label={`${sign}${safeValue}`}
    >
      <span className="inline-flex" aria-hidden>
        {sign}
        <AnimatedNumber value={safeValue} />
      </span>
      <span className="sr-only">
        {sign}
        {safeValue}
      </span>
    </span>
  );
}

function AnimatedNumber({ value }: { value: number }) {
  const safeValue = Number.isFinite(value) ? Math.max(0, Math.round(value)) : 0;
  const [display, setDisplay] = useState(0);
  const displayRef = useRef(0);

  const setAnimatedDisplay = useCallback((next: number) => {
    displayRef.current = next;
    setDisplay(next);
  }, []);

  useEffect(() => {
    const reduceMotion = window.matchMedia?.(
      "(prefers-reduced-motion: reduce)",
    ).matches;
    if (reduceMotion) {
      setAnimatedDisplay(safeValue);
      return;
    }
    const start = displayRef.current;
    const delta = safeValue - start;
    if (delta === 0) {
      setAnimatedDisplay(safeValue);
      return;
    }
    const duration = 260;
    const startedAt = performance.now();
    let frame = 0;
    const tick = (now: number) => {
      const progress = Math.min(1, (now - startedAt) / duration);
      const eased = 1 - Math.pow(1 - progress, 3);
      setAnimatedDisplay(Math.round(start + delta * eased));
      if (progress < 1) {
        frame = window.requestAnimationFrame(tick);
        return;
      }
      displayRef.current = safeValue;
    };
    frame = window.requestAnimationFrame(tick);
    return () => window.cancelAnimationFrame(frame);
  }, [safeValue, setAnimatedDisplay]);

  return <RollingNumber value={display} />;
}

function RollingNumber({ value }: { value: number }) {
  const digits = String(value).split("");
  return (
    <span
      className="inline-flex h-[1em] overflow-hidden align-[-0.13em]"
      aria-hidden
    >
      {digits.map((digit, index) => (
        <RollingDigit key={`${digits.length}-${index}`} digit={Number(digit)} />
      ))}
    </span>
  );
}

function RollingDigit({ digit }: { digit: number }) {
  const safeDigit = Number.isFinite(digit)
    ? Math.min(9, Math.max(0, digit))
    : 0;
  return (
    <span className="relative inline-block h-[1em] w-[0.62em] overflow-hidden">
      <span
        className="flex flex-col transition-transform duration-200 ease-out will-change-transform"
        style={{ transform: `translateY(-${safeDigit}em)` }}
      >
        {Array.from({ length: 10 }, (_, n) => (
          <span key={n} className="block h-[1em] leading-none">
            {n}
          </span>
        ))}
      </span>
    </span>
  );
}
